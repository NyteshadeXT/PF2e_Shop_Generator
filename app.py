# app.py — production-ready Flask application (Render + Player View)

from flask import (
    Flask, render_template, request, redirect, abort,
    current_app, jsonify, url_for
)

import os, secrets, uuid, sqlite3
from pathlib import Path

# Third-party
from werkzeug.exceptions import HTTPException

# Core project imports from the services package
from services.db import load_items
from services.logic import CONFIG as LOGIC_CONFIG
from services.utils import aon_url
from services.spellbooks import build_spellbook
from services.generation import (
    GenerationInputError,
    generate_shop_snapshot,
    get_shop_types,
)
from services.magic_builder import bp as magic_builder_bp
from services.curation import bp as curation_bp
from services.player_views import (
    DuplicateGeneration,
    generation_request_snapshot,
    normalize_channel,
    initialize as initialize_player_views,
    save_snapshot as save_persistent_snapshot,
    state_db_path,
)
from services.player_view_routes import register_routes as register_player_view_routes
from services.web_security import configure_web_security, environment_flag

# Optional: debug blueprint (if exists)
try:
    from services.debug import bp as debug_bp
except Exception:
    debug_bp = None

app = Flask(__name__)
configure_web_security(
    app,
    project_root=Path(__file__).resolve().parent,
    state_database=state_db_path(),
)
if debug_bp is not None and environment_flag("LOOTGEN_ENABLE_DEBUG_ROUTES"):
    app.register_blueprint(debug_bp, url_prefix="/debug")
app.register_blueprint(magic_builder_bp)
app.register_blueprint(curation_bp)
register_player_view_routes(app)

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
    
@app.get("/")
def index():
    df = load_items()
    shop_types = get_shop_types(df)
    dispositions = [
        value
        for value in LOGIC_CONFIG.get("disposition_multipliers", {}).keys()
        if value != "fair"
    ] or ["standard"]
    level_caps = LOGIC_CONFIG.get("level_caps", {"min": 1, "max": 20})
    return render_template(
        "index.html",
        shop_types=shop_types,
        shop_type=None,
        shop_size="medium",
        disposition="standard",
        dispositions=dispositions,
        disposition_labels={
            "very_generous": "1 — Very Generous (80%)",
            "generous": "2 — Generous (90%)",
            "standard": "3 — Standard Pricing (100%)",
            "greedy": "4 — Greedy (115%)",
            "very_greedy": "5 — Very Greedy (130%)",
        },
        party_level=5,
        party_level_min=int(level_caps.get("min", 1)),
        party_level_max=int(level_caps.get("max", 20)),
        seed="",
        generation_request_key=secrets.token_urlsafe(24),
        curate_roll=str(request.args.get("curate_roll") or "").strip(),
        curate_channel=str(request.args.get("curate_channel") or "").strip(),
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
