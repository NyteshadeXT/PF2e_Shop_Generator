"""Small HTTP smoke test for the production Gunicorn configuration."""
from __future__ import annotations

import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)


def _request(base_url: str, path: str) -> tuple[int, bytes, object]:
    request = Request(
        urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
        headers={"User-Agent": "PF2e-production-smoke/1"},
    )
    try:
        with _OPENER.open(request, timeout=10) as response:
            return response.status, response.read(), response.headers
    except HTTPError as error:
        return error.code, error.read(), error.headers


def _expect(base_url: str, path: str, expected_status: int):
    status, body, headers = _request(base_url, path)
    if status != expected_status:
        raise AssertionError(
            f"{path} returned HTTP {status}; expected {expected_status}. "
            f"Body starts with {body[:200]!r}"
        )
    return body, headers


def _wait_until_ready(base_url: str, timeout_seconds: float = 30.0):
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            body, headers = _expect(base_url, "/health", 200)
            return body, headers
        except (AssertionError, OSError, URLError) as error:
            last_error = error
            time.sleep(0.25)
    raise AssertionError(f"Gunicorn did not become ready: {last_error}")


def run(base_url: str) -> None:
    health_body, health_headers = _wait_until_ready(base_url)
    health = json.loads(health_body)
    if health != {
        "ok": True,
        "checks": {"catalog": True, "player_view_storage": True},
    }:
        raise AssertionError(f"Unexpected health response: {health!r}")

    _root_body, root_headers = _expect(base_url, "/", 302)
    if "/gm-login" not in str(root_headers.get("Location") or ""):
        raise AssertionError("Protected generator did not redirect to GM login.")

    login_body, login_headers = _expect(base_url, "/gm-login", 200)
    if b"GM Access" not in login_body:
        raise AssertionError("GM login page did not render.")

    css_body, css_headers = _expect(base_url, "/static/pf2e.css", 200)
    if not css_body or "text/css" not in str(css_headers.get("Content-Type") or ""):
        raise AssertionError("Primary stylesheet was not served as CSS.")

    missing_body, _missing_headers = _expect(
        base_url,
        "/player-view?channel=smoke&roll_id=" + ("0" * 32),
        404,
    )
    if b"no longer available" not in missing_body.lower():
        raise AssertionError("Missing Player View did not return the expected safe page.")

    policy = str(login_headers.get("Content-Security-Policy") or "")
    if "script-src 'self' 'nonce-" not in policy or "object-src 'none'" not in policy:
        raise AssertionError("Production Content Security Policy is missing.")
    if not health_headers.get("Strict-Transport-Security"):
        raise AssertionError("Render-mode HSTS header is missing.")

    print("Production smoke checks passed.")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765")
