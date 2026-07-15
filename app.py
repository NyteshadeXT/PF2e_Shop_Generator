# app.py — production-ready Flask application (Render + Player View)

from flask import (
    Flask, render_template, request, redirect, abort, session,
    current_app, g, jsonify, send_file, url_for
)

from io import BytesIO
import hashlib, json, os, secrets, uuid, sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

# Third-party
from werkzeug.exceptions import HTTPException

# Core project imports from the services package
from services.db import load_items
from services.logic import CONFIG as LOGIC_CONFIG
from services.utils import rarity_counts, aon_url
from services.spellbooks import build_spellbook
from services.generation import (
    GenerationInputError,
    build_payload as _build_payload,
    count_critical as _count_crit,
    generate_shop_snapshot,
    get_shop_types,
)
from services.magic_builder import bp as magic_builder_bp
from services.player_views import (
    backup_database as backup_player_views,
    channel_summaries,
    channel_state,
    DuplicateGeneration,
    generation_request_snapshot,
    current_token as persistent_current_token,
    LiveChannelNotFound,
    SnapshotNotFound,
    live_channel as persistent_live_channel,
    load_snapshot as load_persistent_snapshot,
    normalize_channel,
    recent_snapshots,
    rotate_live_token,
    initialize as initialize_player_views,
    save_snapshot as save_persistent_snapshot,
    set_current_snapshot,
    snapshot_count,
    state_db_path,
)
from services.security import SQLiteAttemptLimiter, load_session_secret

# Optional: debug blueprint (if exists)
try:
    from services.debug import bp as debug_bp
except Exception:
    debug_bp = None

def _environment_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_int_environment(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


app = Flask(__name__)
session_secret_file = os.environ.get("LOOTGEN_SESSION_SECRET_FILE", "").strip()
if session_secret_file:
    session_secret_path = Path(session_secret_file).expanduser()
    if not session_secret_path.is_absolute():
        session_secret_path = Path(__file__).resolve().parent / session_secret_path
else:
    session_secret_path = state_db_path().parent / ".lootgen-session-secret"
app.config.update(
    SECRET_KEY=load_session_secret(
        os.environ.get("LOOTGEN_SESSION_SECRET"),
        session_secret_path,
    ),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_environment_flag("RENDER") or _environment_flag("LOOTGEN_SECURE_COOKIES"),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
_login_limiter = SQLiteAttemptLimiter(
    _positive_int_environment("LOOTGEN_LOGIN_ATTEMPTS", 8),
    _positive_int_environment("LOOTGEN_LOGIN_WINDOW_SECONDS", 300),
    state_db_path(),
)
try:
    app.config["MAX_CONTENT_LENGTH"] = max(
        64 * 1024, int(os.environ.get("LOOTGEN_MAX_REQUEST_BYTES", 2 * 1024 * 1024))
    )
except (TypeError, ValueError):
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
if debug_bp is not None and _environment_flag("LOOTGEN_ENABLE_DEBUG_ROUTES"):
    app.register_blueprint(debug_bp, url_prefix="/debug")
app.register_blueprint(magic_builder_bp)


def _gm_access_key() -> str:
    return os.environ.get("LOOTGEN_GM_ACCESS_KEY", "").strip()


def _access_fingerprint(access_key: str) -> str:
    return hashlib.sha256(access_key.encode("utf-8")).hexdigest()


def _gm_is_authenticated() -> bool:
    access_key = _gm_access_key()
    if not access_key:
        return True
    saved = str(session.get("gm_access") or "")
    return secrets.compare_digest(saved, _access_fingerprint(access_key))


def _csrf_token() -> str:
    token = str(session.get("csrf_token") or "")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _csp_nonce() -> str:
    nonce = str(getattr(g, "csp_nonce", "") or "")
    if not nonce:
        nonce = secrets.token_urlsafe(18)
        g.csp_nonce = nonce
    return nonce


_CSRF_PROTECTED_ENDPOINTS = {
    "gm_login",
    "gm_logout",
    "query",
    "publish_player_view",
    "history_make_live",
    "history_backup",
    "history_rotate_live",
}


_PUBLIC_ENDPOINTS = {
    "static",
    "favicon",
    "health",
    "gm_login",
    "player_view",
    "live_view",
    "live_version",
}


@app.before_request
def require_gm_access():
    if not _gm_access_key() or request.endpoint in _PUBLIC_ENDPOINTS or _gm_is_authenticated():
        return None
    if request.path.startswith("/api/"):
        return _json_error_response(401, "GM access is required.")
    next_path = request.full_path.rstrip("?") if request.method == "GET" else url_for("index")
    return redirect(url_for("gm_login", next=next_path))


@app.before_request
def require_csrf_token():
    if request.method != "POST" or request.endpoint not in _CSRF_PROTECTED_ENDPOINTS:
        return None
    if app.config.get("TESTING") and not app.config.get("CSRF_PROTECTION_IN_TESTS"):
        return None
    submitted = str(request.form.get("csrf_token") or "")
    expected = str(session.get("csrf_token") or "")
    if not submitted or not expected or not secrets.compare_digest(submitted, expected):
        abort(400, "The form expired or came from another site. Reload the page and try again.")
    return None


@app.route("/gm-login", methods=["GET", "POST"])
def gm_login():
    if not _gm_access_key():
        return redirect(url_for("index"))
    error = None
    next_path = str(request.values.get("next") or url_for("index"))
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = url_for("index")
    if request.method == "POST":
        client = request.remote_addr or "unknown"
        if _login_limiter.blocked(client):
            abort(429, "Too many unsuccessful login attempts. Wait a few minutes and try again.")
        submitted = str(request.form.get("access_key") or "")
        if secrets.compare_digest(submitted, _gm_access_key()):
            _login_limiter.clear(client)
            session.clear()
            session["gm_access"] = _access_fingerprint(_gm_access_key())
            session.permanent = True
            return redirect(next_path)
        _login_limiter.record_failure(client)
        error = "That access key was not accepted."
    return render_template("gm_login.html", error=error, next_path=next_path), 401 if error else 200


@app.post("/gm-logout")
def gm_logout():
    session.clear()
    return redirect(url_for("gm_login"))


app.jinja_env.globals["gm_access_enabled"] = lambda: bool(_gm_access_key())
app.jinja_env.globals["csrf_token"] = _csrf_token
app.jinja_env.globals["csp_nonce"] = _csp_nonce


@app.after_request
def add_security_headers(response):
    nonce = _csp_nonce()
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "; ".join(
            (
                "default-src 'self'",
                f"script-src 'self' 'nonce-{nonce}'",
                "script-src-attr 'none'",
                "style-src 'self'",
                "style-src-attr 'none'",
                "img-src 'self' data:",
                "font-src 'self'",
                "connect-src 'self'",
                "object-src 'none'",
                "base-uri 'none'",
                "form-action 'self'",
                "frame-ancestors 'self'",
            )
        ),
    )
    if request.is_secure or _environment_flag("RENDER"):
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response

_ERROR_TITLES = {
    400: "Check the submitted information",
    404: "Page not found",
    405: "Action not available",
    413: "Submission too large",
    429: "Too many attempts",
    500: "Something went wrong",
    503: "Service temporarily unavailable",
}


def _json_error_response(status_code: int, message: str):
    response = jsonify(ok=False, error=message)
    response.headers["Cache-Control"] = "no-store"
    return response, status_code


@app.errorhandler(HTTPException)
def handle_http_error(error: HTTPException):
    status_code = int(error.code or 500)
    message = str(error.description or _ERROR_TITLES.get(status_code, "Request failed."))
    if request.path.startswith("/api/"):
        return _json_error_response(status_code, message)
    return render_template(
        "error.html",
        status_code=status_code,
        error_title=_ERROR_TITLES.get(status_code, "Request failed"),
        error_message=message,
    ), status_code


@app.errorhandler(Exception)
def handle_unexpected_error(error: Exception):
    current_app.logger.exception("Unhandled request failure")
    message = "The generator encountered an unexpected problem. Please try again."
    if request.path.startswith("/api/"):
        return _json_error_response(500, message)
    return render_template(
        "error.html",
        status_code=500,
        error_title=_ERROR_TITLES[500],
        error_message=message,
    ), 500

# Make AoN helper available in Jinja
app.jinja_env.globals["aon_url"] = aon_url

# use the already-imported LOGIC_CONFIG from services.logic
DB_PATH = LOGIC_CONFIG.get("sqlite_db_path", "data/pf2e.sqlite")

# ----------------------------
# Helper utilities
# ---------------------------- 
def _norm_str(x):
    if x is None:
        return ""
    try:
        return str(x).strip()
    except Exception:
        return ""


def _open_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _row_has_all_themes(row_traits: str, themes: list[str]) -> bool:
    """Case-insensitive AND filter: all theme terms must appear in the spell's traits"""
    if not themes:
        return True
    traits = [t.strip().lower() for t in (row_traits or "").split(",")]
    themes_norm = [x.strip().lower() for x in themes if x.strip()]
    return all(any(theme in tr for tr in traits) for theme in themes_norm)

@app.get("/live/<live_token>")
def live_view(live_token: str):
    """Stable player-facing URL that follows a campaign's newest snapshot."""
    try:
        state = persistent_live_channel(live_token)
    except (LiveChannelNotFound, ValueError):
        return render_template("player_view_missing.html"), 404
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Unable to resolve live Player View")
        abort(503, "Live Player View storage is temporarily unavailable.")
    return redirect(
        url_for(
            "player_view",
            channel=state["channel"],
            roll_id=state["roll_id"],
            live=live_token,
        )
    )


@app.get("/api/live/<live_token>/version")
def live_version(live_token: str):
    """Persistent polling endpoint; safe across workers and service restarts."""
    try:
        state = persistent_live_channel(live_token)
    except (LiveChannelNotFound, ValueError):
        abort(404)
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Unable to poll live Player View")
        abort(503)
    etag = hashlib.sha256(
        f'{state["channel"]}:{state["roll_id"]}'.encode("utf-8")
    ).hexdigest()
    if request.if_none_match.contains(etag):
        response = current_app.response_class(status=304)
    else:
        response = jsonify(state)
    response.set_etag(etag)
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@app.get("/health")
def health():
    checks = {"catalog": False, "player_view_storage": False}
    try:
        df = load_items()
        checks["catalog"] = df is not None and not df.empty
    except Exception as exc:
        current_app.logger.warning("Catalog readiness check failed: %s", exc)
    try:
        initialize_player_views()
        checks["player_view_storage"] = True
    except (OSError, sqlite3.Error) as exc:
        current_app.logger.warning("Player View storage readiness check failed: %s", exc)
    ready = all(checks.values())
    response = jsonify(ok=ready, checks=checks)
    response.headers["Cache-Control"] = "no-store"
    return response, 200 if ready else 503



# ----------------------------
# Routes
# ----------------------------
@app.get("/favicon.ico")
def favicon():
    return ("", 204)
    
@app.get("/player-view")
def player_view():
    data = request.values
    try:
        channel = normalize_channel(data.get("channel"))
    except ValueError as exc:
        abort(400, str(exc))
    roll_id = (data.get("roll_id") or "").strip()
    live_token = (request.args.get("live") or "").strip().lower()
    if live_token:
        try:
            live_state = persistent_live_channel(live_token)
        except (LiveChannelNotFound, ValueError):
            return render_template("player_view_missing.html"), 404
        except (OSError, sqlite3.Error):
            abort(503, "Live Player View storage is temporarily unavailable.")
        if live_state["channel"] != channel:
            abort(404)

    if not roll_id:
        return redirect("/")

    lists: dict = {}
    meta: dict = {}
    snapshot_payload: dict | None = None

    try:
        snapshot_payload = load_persistent_snapshot(roll_id, channel)
    except SnapshotNotFound:
        return render_template(
            "player_view_missing.html", channel=channel, roll_id=roll_id
        ), 404
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
        current_app.logger.exception("Unable to load Player View snapshot")
        abort(503, "Player View storage is temporarily unavailable.")

    if isinstance(snapshot_payload, dict) and snapshot_payload:
        if "lists" in snapshot_payload or "shop" in snapshot_payload:
            lists = snapshot_payload.get("lists") or {}
            meta = snapshot_payload.get("shop") or {}
        else:
            # tolerate a flat snapshot (legacy format)
            lists = {
                "mundane_items":  snapshot_payload.get("mundane_items", []),
                "material_items": snapshot_payload.get("material_items", []) or snapshot_payload.get("materials_items", []),
                "armor_items":    snapshot_payload.get("armor_items", []),
                "weapon_items":   snapshot_payload.get("weapon_items", []),
                "magic_items":    snapshot_payload.get("magic_items", []),
                "formula_items":  snapshot_payload.get("formula_items", []),
            }
            meta = {
                "shop_type":   snapshot_payload.get("shop_type"),
                "shop_size":   snapshot_payload.get("shop_size"),
                "disposition": snapshot_payload.get("disposition"),
                "party_level": snapshot_payload.get("party_level"),
                "seed":        snapshot_payload.get("seed"),
                "window":      snapshot_payload.get("window"),
            }

    if not lists:
        abort(400, "Player View snapshot is missing or invalid.")

    shop_name = _norm_str(meta.get("shop_name") or meta.get("name"))
    if not shop_name:
        shop_name = None

    default_label = (_norm_str(meta.get("shop_type")) or "Shop").title()
    page_title = f"Player View — {shop_name or default_label}"
    return render_template(
        "results_player.html",   # ensure this file exists in templates/
        page_title=page_title,
        shop_name=shop_name,
        shop_type=meta.get("shop_type"),
        seed=meta.get("seed"),
        mundane_items=lists.get("mundane_items", []),
        material_items=lists.get("material_items", []),
        armor_items=lists.get("armor_items", []),
        weapon_items=lists.get("weapon_items", []),
        magic_items=lists.get("magic_items", []),
        formula_items=lists.get("formula_items", []),
        aon_url=aon_url,
        channel=channel,
        roll_id=roll_id,
        live_token=live_token,
    )

@app.get("/")
def index():
    df = load_items()
    shop_types = get_shop_types(df)
    dispositions = list(LOGIC_CONFIG.get("disposition_multipliers", {}).keys()) or ["fair"]
    level_caps = LOGIC_CONFIG.get("level_caps", {"min": 1, "max": 20})
    return render_template(
        "index.html",
        shop_types=shop_types,
        shop_type=None,
        shop_size="medium",
        disposition="fair",
        dispositions=dispositions,
        party_level=5,
        party_level_min=int(level_caps.get("min", 1)),
        party_level_max=int(level_caps.get("max", 20)),
        seed="",
        generation_request_key=secrets.token_urlsafe(24),
    )

@app.post("/query")
def query():
    data = request.values  # supports both .args (GET) and .form (POST)
    generation_request_key = str(data.get("generation_request_key") or "").strip()
    if generation_request_key:
        try:
            existing_generation = generation_request_snapshot(generation_request_key)
        except ValueError as exc:
            abort(400, str(exc))
        except (OSError, sqlite3.Error):
            current_app.logger.exception("Unable to check generation request key")
            abort(503, "Player View storage is temporarily unavailable.")
        if existing_generation:
            return redirect(
                url_for(
                    "results_view",
                    channel=existing_generation["channel"],
                    roll_id=existing_generation["token"],
                ),
                code=303,
            )
    df = load_items()
    try:
        snapshot = generate_shop_snapshot(df, data.to_dict(flat=True))
    except (GenerationInputError, ValueError) as exc:
        abort(400, str(exc))
    try:
        channel = normalize_channel(data.get("channel"))
    except ValueError as exc:
        abort(400, str(exc))
    roll_id = uuid.uuid4().hex
    try:
        # The stored snapshot is authoritative. Notify live views only after commit.
        # Keep an existing Live Display on its currently published shop until the
        # GM deliberately publishes this new snapshot. A game's first shop still
        # establishes the channel and its stable live link.
        save_persistent_snapshot(
            roll_id,
            channel,
            snapshot,
            advance_channel=False,
            generation_key=generation_request_key or None,
        )
    except DuplicateGeneration as duplicate:
        return redirect(
            url_for(
                "results_view", channel=duplicate.channel, roll_id=duplicate.token
            ),
            code=303,
        )
    except (OSError, sqlite3.Error, ValueError, TypeError):
        current_app.logger.exception("Unable to persist generated Player View snapshot")
        abort(503, "Player View storage is temporarily unavailable.")

    return redirect(
        url_for("results_view", channel=channel, roll_id=roll_id), code=303
    )


@app.get("/results/<roll_id>")
def results_view(roll_id: str):
    """Render a stored GM result without repeating the generation POST."""
    try:
        channel = normalize_channel(request.args.get("channel"))
    except ValueError as exc:
        abort(400, str(exc))
    try:
        snapshot = load_persistent_snapshot(roll_id, channel)
        live_state = channel_state(channel)
    except (SnapshotNotFound, LiveChannelNotFound):
        abort(404, "That generated shop is no longer available.")
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
        current_app.logger.exception("Unable to load generated shop result")
        abort(503, "Player View storage is temporarily unavailable.")

    meta = snapshot.get("shop") or {}
    lists = snapshot.get("lists") or {}
    summary = snapshot.get("summary") or {}
    all_items = (
        list(lists.get("mundane_items") or [])
        + list(lists.get("material_items") or [])
        + list(lists.get("armor_items") or [])
        + list(lists.get("weapon_items") or [])
        + list(lists.get("magic_items") or [])
    )
    counts = summary.get("counts") or rarity_counts(all_items)
    picked = summary.get("picked") or {
        "mundane": len(lists.get("mundane_items") or []),
        "materials": len(lists.get("material_items") or []),
        "armor": len(lists.get("armor_items") or []),
        "weapons": len(lists.get("weapon_items") or []),
        "magic": len(lists.get("magic_items") or []),
        "formulas": len(lists.get("formula_items") or []),
        "critical": _count_crit(all_items),
    }
    return render_template(
        "results.html",
        shop_type=meta.get("shop_type"),
        shop_size=meta.get("shop_size"),
        disposition=meta.get("disposition"),
        shop_name=meta.get("shop_name"),
        party_level=meta.get("party_level"),
        seed=meta.get("seed"),
        reproduction_key=meta.get("reproduction_key"),
        generation_fingerprint=meta.get("generation_fingerprint"),
        reproduction_warning=summary.get("reproduction_warning", ""),
        picked=picked,
        counts=counts,
        mundane_items=lists.get("mundane_items", []),
        material_items=lists.get("material_items", []),
        armor_items=lists.get("armor_items", []),
        weapon_items=lists.get("weapon_items", []),
        magic_items=lists.get("magic_items", []),
        formula_items=lists.get("formula_items", []),
        aon_url=aon_url,
        window=meta.get("window"),
        roll_id=roll_id,
        channel=channel,
        live_token=live_state["live_token"],
        is_live=live_state["current_token"] == roll_id,
        generation_request_key=secrets.token_urlsafe(24),
    )


@app.post("/player-view/publish")
def publish_player_view():
    """Publish a prepared immutable snapshot to its game's Live Display."""
    try:
        channel = normalize_channel(request.form.get("channel"))
        token = str(request.form.get("roll_id") or "").strip()
        set_current_snapshot(token, channel)
    except ValueError as exc:
        abort(400, str(exc))
    except SnapshotNotFound:
        abort(404, "That stored Player View no longer exists.")
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Unable to publish Player View")
        abort(503, "Player View storage is temporarily unavailable.")
    return redirect(url_for("player_view", channel=channel, roll_id=token, published="1"))


@app.get("/history")
def history():
    channel = str(request.args.get("channel") or "").strip()
    try:
        page = int(request.args.get("page") or 1)
        if page < 1:
            raise ValueError("History page must be a positive whole number.")
        per_page = 50
        total_snapshots = snapshot_count(channel=channel or None)
        page_count = max(1, (total_snapshots + per_page - 1) // per_page)
        page = min(page, page_count)
        snapshots = recent_snapshots(
            channel=channel or None,
            limit=per_page,
            offset=(page - 1) * per_page,
        )
        channels = channel_summaries()
    except ValueError as exc:
        abort(400, str(exc))
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Unable to load recent Player Views")
        abort(503, "Player View storage is temporarily unavailable.")
    return render_template(
        "history.html",
        snapshots=snapshots,
        selected_channel=channel.lower(),
        channels=channels,
        total_snapshots=total_snapshots,
        page=page,
        page_count=page_count,
        first_result=((page - 1) * per_page + 1) if total_snapshots else 0,
        last_result=min(page * per_page, total_snapshots),
    )


@app.post("/history/backup")
def history_backup():
    """Download an integrity-checked copy of persistent Player View storage."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    download_name = f"pf2e-player-views-{timestamp}.db"
    try:
        with tempfile.TemporaryDirectory(prefix="pf2e-player-view-backup-") as directory:
            backup_path = backup_player_views(Path(directory) / download_name)
            download = BytesIO(backup_path.read_bytes())
    except (OSError, sqlite3.Error, ValueError):
        current_app.logger.exception("Unable to create downloadable Player View backup")
        abort(503, "A Player View backup could not be created right now.")
    download.seek(0)
    response = send_file(
        download,
        as_attachment=True,
        download_name=download_name,
        mimetype="application/vnd.sqlite3",
        conditional=False,
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/history/make-live")
def history_make_live():
    try:
        channel = normalize_channel(request.form.get("channel"))
        token = str(request.form.get("roll_id") or "").strip()
        set_current_snapshot(token, channel)
    except ValueError as exc:
        abort(400, str(exc))
    except SnapshotNotFound:
        abort(404, "That stored Player View no longer exists.")
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Unable to restore live Player View")
        abort(503, "Player View storage is temporarily unavailable.")
    return redirect(url_for("history", channel=channel, restored=token))


@app.post("/history/rotate-live")
def history_rotate_live():
    try:
        channel = normalize_channel(request.form.get("channel"))
        rotate_live_token(channel)
    except ValueError as exc:
        abort(400, str(exc))
    except LiveChannelNotFound:
        abort(404, "That Live Display no longer exists.")
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Unable to rotate Live Display link")
        abort(503, "Player View storage is temporarily unavailable.")
    return redirect(url_for("history", channel=channel, rotated="1"))

# Standalone Spellbook page
@app.get("/spellbooks")
def spellbooks_page():
    return render_template(
        "spellbook_page.html",
        max_level=1,
        aon_url=aon_url,
    )

# JSON → fragment API for both the page and the mini-tool on index.html
@app.post("/api/spellbooks/generate")
def api_generate_spellbook():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(ok=False, error="JSON body must be an object"), 400

    tradition = (data.get("tradition") or "").strip().title()
    if tradition not in ("Arcane", "Divine", "Occult", "Primal"):
        return jsonify(ok=False, error="Invalid or missing tradition"), 400

    try:
        max_level = max(1, min(10, int(data.get("max_level") or 1)))
    except Exception:
        max_level = 1

    # themes can be a list or comma-separated string; normalize to list[str]
    raw_themes = data.get("themes") or []
    if isinstance(raw_themes, str):
        themes = [t.strip() for t in raw_themes.split(",") if t.strip()]
    else:
        themes = [str(t).strip() for t in raw_themes if str(t).strip()]

    # Use the library function to build a book directly
    spells = build_spellbook(tradition=tradition, book_level=max_level, themes=themes)

    return jsonify(ok=True, spells=spells)

@app.get("/spellbooks/view")
def spellbooks_view():
    tradition = (request.args.get("tradition") or "").strip().title()
    if tradition not in ("Arcane", "Divine", "Occult", "Primal"):
        abort(400, "Invalid or missing tradition.")
    try:
        max_level = int(request.args.get("max_level") or 1)
    except (TypeError, ValueError):
        abort(400, "Maximum spell rank must be a whole number.")
    if not 1 <= max_level <= 10:
        abort(400, "Maximum spell rank must be between 1 and 10.")

    themes_raw = request.args.get("themes") or ""
    themes = [t.strip() for t in themes_raw.split(",") if t.strip()]

    spells = build_spellbook(tradition=tradition, book_level=max_level, themes=themes)

    return render_template(
        "spellbook_page.html",
        spells=spells,
        tradition=tradition,
        max_level=max_level,
        themes=themes,
        aon_url=aon_url,
    )


if __name__ == "__main__":
    # For local development. Render should start the app with gunicorn.
    debug = os.environ.get("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, debug=debug)
