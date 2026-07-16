"""Authentication, session, CSRF, and browser-security configuration."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import os
from pathlib import Path
import secrets

from flask import (
    Flask,
    abort,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from services.security import SQLiteAttemptLimiter, load_session_secret


CSRF_PROTECTED_ENDPOINTS = frozenset(
    {
        "gm_login",
        "gm_logout",
        "query",
        "publish_player_view",
        "history_make_live",
        "history_backup",
        "history_rotate_live",
        "history_update_metadata",
        "history_archive",
        "history_delete",
        "curation.curate_snapshot",
    }
)

PUBLIC_ENDPOINTS = frozenset(
    {
        "static",
        "favicon",
        "health",
        "gm_login",
        "player_view",
        "live_view",
        "live_version",
    }
)

_login_limiter = None


def environment_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_environment_integer(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


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


def csrf_token() -> str:
    token = str(session.get("csrf_token") or "")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def csp_nonce() -> str:
    nonce = str(getattr(g, "csp_nonce", "") or "")
    if not nonce:
        nonce = secrets.token_urlsafe(18)
        g.csp_nonce = nonce
    return nonce


def _api_authentication_error():
    response = jsonify(ok=False, error="GM access is required.")
    response.headers["Cache-Control"] = "no-store"
    return response, 401


def require_gm_access():
    if not _gm_access_key() or request.endpoint in PUBLIC_ENDPOINTS or _gm_is_authenticated():
        return None
    if request.path.startswith("/api/"):
        return _api_authentication_error()
    next_path = request.full_path.rstrip("?") if request.method == "GET" else url_for("index")
    return redirect(url_for("gm_login", next=next_path))


def require_csrf_token():
    if request.method != "POST" or request.endpoint not in CSRF_PROTECTED_ENDPOINTS:
        return None
    if current_app.config.get("TESTING") and not current_app.config.get(
        "CSRF_PROTECTION_IN_TESTS"
    ):
        return None
    submitted = str(request.form.get("csrf_token") or "")
    expected = str(session.get("csrf_token") or "")
    if not submitted or not expected or not secrets.compare_digest(submitted, expected):
        abort(400, "The form expired or came from another site. Reload the page and try again.")
    return None


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
        access_key = _gm_access_key()
        if secrets.compare_digest(submitted, access_key):
            _login_limiter.clear(client)
            session.clear()
            session["gm_access"] = _access_fingerprint(access_key)
            session.permanent = True
            return redirect(next_path)
        _login_limiter.record_failure(client)
        error = "That access key was not accepted."
    return render_template("gm_login.html", error=error, next_path=next_path), 401 if error else 200


def gm_logout():
    session.clear()
    return redirect(url_for("gm_login"))


def add_security_headers(response):
    nonce = csp_nonce()
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
    if request.is_secure or environment_flag("RENDER"):
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


def configure_web_security(app: Flask, *, project_root: Path, state_database: Path) -> None:
    """Configure shared sessions and register the established security endpoints."""
    global _login_limiter

    configured_secret_file = os.environ.get("LOOTGEN_SESSION_SECRET_FILE", "").strip()
    if configured_secret_file:
        secret_path = Path(configured_secret_file).expanduser()
        if not secret_path.is_absolute():
            secret_path = project_root / secret_path
    else:
        secret_path = state_database.parent / ".lootgen-session-secret"

    app.config.update(
        SECRET_KEY=load_session_secret(os.environ.get("LOOTGEN_SESSION_SECRET"), secret_path),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=environment_flag("RENDER")
        or environment_flag("LOOTGEN_SECURE_COOKIES"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    try:
        app.config["MAX_CONTENT_LENGTH"] = max(
            64 * 1024,
            int(os.environ.get("LOOTGEN_MAX_REQUEST_BYTES", 2 * 1024 * 1024)),
        )
    except (TypeError, ValueError):
        app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

    _login_limiter = SQLiteAttemptLimiter(
        _positive_environment_integer("LOOTGEN_LOGIN_ATTEMPTS", 8),
        _positive_environment_integer("LOOTGEN_LOGIN_WINDOW_SECONDS", 300),
        state_database,
    )

    app.before_request(require_gm_access)
    app.before_request(require_csrf_token)
    app.after_request(add_security_headers)
    app.add_url_rule("/gm-login", "gm_login", gm_login, methods=["GET", "POST"])
    app.add_url_rule("/gm-logout", "gm_logout", gm_logout, methods=["POST"])
    app.jinja_env.globals["gm_access_enabled"] = lambda: bool(_gm_access_key())
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["csp_nonce"] = csp_nonce
