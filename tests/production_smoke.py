"""Small HTTP smoke test for the production Gunicorn configuration."""
from __future__ import annotations

import json
import re
import sys
import time
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# CI talks directly to Gunicorn over HTTP while RENDER mode correctly marks the
# browser cookie Secure. Treat this loopback transport as HTTPS in the test jar.
_COOKIE_POLICY = DefaultCookiePolicy(secure_protocols=("http", "https", "wss"))
_OPENER = build_opener(
    _NoRedirect, HTTPCookieProcessor(CookieJar(policy=_COOKIE_POLICY))
)


def _request(
    base_url: str,
    path: str,
    *,
    form: dict[str, str] | None = None,
    json_body: dict | None = None,
) -> tuple[int, bytes, object]:
    data = None
    headers = {"User-Agent": "PF2e-production-smoke/1"}
    if form is not None:
        data = urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
        data=data,
        headers=headers,
    )
    try:
        with _OPENER.open(request, timeout=30) as response:
            return response.status, response.read(), response.headers
    except HTTPError as error:
        return error.code, error.read(), error.headers


def _expect(base_url: str, path: str, expected_status: int, **request_options):
    status, body, headers = _request(base_url, path, **request_options)
    if status != expected_status:
        raise AssertionError(
            f"{path} returned HTTP {status}; expected {expected_status}. "
            f"Body starts with {body[:200]!r}"
        )
    return body, headers


def _hidden_value(body: bytes, name: str) -> str:
    text = body.decode("utf-8")
    match = re.search(
        rf'<input[^>]+name=["\']{re.escape(name)}["\'][^>]+value=["\']([^"\']*)',
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        raise AssertionError(f"Response did not contain hidden field {name!r}.")
    return match.group(1)


def _result_location(headers) -> tuple[str, str, str]:
    location = str(headers.get("Location") or "")
    parsed = urlparse(location)
    match = re.fullmatch(r"/results/([a-f0-9]{32})", parsed.path)
    channel = parse_qs(parsed.query).get("channel", [""])[0]
    if not match or not channel:
        raise AssertionError(f"Unexpected generated-result location: {location!r}")
    return location, match.group(1), channel


def _live_token(body: bytes) -> str:
    match = re.search(rb'href=["\']/live/([a-z0-9]+)', body)
    if not match:
        raise AssertionError("Generated result did not expose its Live Display link.")
    return match.group(1).decode("ascii")


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

    login_csrf = _hidden_value(login_body, "csrf_token")
    for _attempt in range(2):
        rejected_body, _rejected_headers = _expect(
            base_url,
            "/gm-login",
            401,
            form={
                "csrf_token": login_csrf,
                "access_key": "incorrect-smoke-key",
                "next": "/",
            },
        )
        if b"not accepted" not in rejected_body:
            raise AssertionError("Invalid GM key did not return the expected safe message.")
    _expect(
        base_url,
        "/gm-login",
        429,
        form={
            "csrf_token": login_csrf,
            "access_key": "smoke-test-private-key",
            "next": "/",
        },
    )
    time.sleep(1.1)
    _accepted_body, accepted_headers = _expect(
        base_url,
        "/gm-login",
        302,
        form={
            "csrf_token": login_csrf,
            "access_key": "smoke-test-private-key",
            "next": "/",
        },
    )
    if str(accepted_headers.get("Location") or "") != "/":
        raise AssertionError("Successful GM login did not return to the generator.")

    index_body, _index_headers = _expect(base_url, "/", 200)
    if b"Generate Inventory" not in index_body:
        raise AssertionError("Authenticated generator page did not render.")
    csrf = _hidden_value(index_body, "csrf_token")
    first_request_key = _hidden_value(index_body, "generation_request_key")
    first_form = {
        "csrf_token": csrf,
        "generation_request_key": first_request_key,
        "shop_type": "General",
        "shop_size": "small",
        "disposition": "fair",
        "shop_name": "Production Smoke Shop One",
        "party_level": "5",
        "channel": "production-smoke",
        "seed": "production-smoke-one",
    }
    _first_post_body, first_post_headers = _expect(
        base_url, "/query", 303, form=first_form
    )
    first_location, first_roll_id, channel = _result_location(first_post_headers)
    first_result, _first_result_headers = _expect(base_url, first_location, 200)
    if b"Production Smoke Shop One" not in first_result:
        raise AssertionError("Generated GM result did not contain the submitted shop.")
    live_token = _live_token(first_result)

    player_path = (
        "/player-view?" + urlencode({"channel": channel, "roll_id": first_roll_id})
    )
    first_player_view, _first_player_headers = _expect(base_url, player_path, 200)
    if b"Production Smoke Shop One" not in first_player_view:
        raise AssertionError("Immutable Player View did not contain the generated shop.")

    bases_body, _bases_headers = _expect(
        base_url,
        "/api/magic-builder/bases?type=weapon&max_level=5",
        200,
    )
    bases = json.loads(bases_body)
    if not bases.get("ok") or not bases.get("names"):
        raise AssertionError("Magic Item Builder returned no weapon bases.")
    built_body, _built_headers = _expect(
        base_url,
        "/api/magic-builder/build",
        200,
        json_body={
            "item_type": "weapon",
            "base_name": bases["names"][0],
            "max_level": 5,
            "reroll": 1,
        },
    )
    built = json.loads(built_body)
    if not built.get("ok") or not (built.get("item") or {}).get("name"):
        raise AssertionError("Magic Item Builder did not return a built weapon.")

    second_form = dict(first_form)
    second_form.update(
        {
            "generation_request_key": "production-smoke-second-request",
            "shop_name": "Production Smoke Shop Two",
            "seed": "production-smoke-two",
        }
    )
    _second_post_body, second_post_headers = _expect(
        base_url, "/query", 303, form=second_form
    )
    second_location, second_roll_id, second_channel = _result_location(
        second_post_headers
    )
    if second_channel != channel or second_roll_id == first_roll_id:
        raise AssertionError("Second generation did not create a draft in the same game.")
    second_result, _second_result_headers = _expect(base_url, second_location, 200)
    if b"players still see the previous live shop" not in second_result:
        raise AssertionError("Second generated shop was not retained as a draft.")
    _publish_body, publish_headers = _expect(
        base_url,
        "/player-view/publish",
        302,
        form={
            "csrf_token": csrf,
            "channel": channel,
            "roll_id": second_roll_id,
        },
    )
    if "/player-view?" not in str(publish_headers.get("Location") or ""):
        raise AssertionError("Publishing did not redirect to the Player View.")
    live_body, _live_headers = _expect(
        base_url, f"/api/live/{live_token}/version", 200
    )
    live_state = json.loads(live_body)
    if live_state.get("roll_id") != second_roll_id:
        raise AssertionError("Stable Live Display did not advance to the published draft.")

    unchanged_first, _unchanged_headers = _expect(base_url, player_path, 200)
    if b"Production Smoke Shop One" not in unchanged_first:
        raise AssertionError("Publishing a draft changed the immutable first Player View.")

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
    required_policy = (
        "script-src 'self' 'nonce-",
        "script-src-attr 'none'",
        "style-src 'self'",
        "style-src-attr 'none'",
        "object-src 'none'",
    )
    if any(directive not in policy for directive in required_policy):
        raise AssertionError("Production Content Security Policy is missing.")
    if "'unsafe-inline'" in policy:
        raise AssertionError("Production Content Security Policy permits inline content.")
    if not health_headers.get("Strict-Transport-Security"):
        raise AssertionError("Render-mode HSTS header is missing.")

    print("Production smoke checks passed.")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765")
