"""Player-facing display, stored-result, and campaign archive routes."""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import tempfile

from flask import (
    Flask,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from services.generation import count_critical
from services.inventory_sections import (
    flattened_inventory,
    inventory_lists,
    legacy_snapshot_lists,
    player_visible_lists,
    section_counts,
    section_template_context,
    template_inventory_context,
)
from services.player_views import (
    LiveChannelNotFound,
    SnapshotNotFound,
    backup_database,
    channel_state,
    channel_summaries,
    delete_snapshot,
    live_channel,
    load_snapshot,
    normalize_channel,
    recent_snapshots,
    rotate_live_token,
    set_current_snapshot,
    set_snapshot_archived,
    snapshot_count,
    update_snapshot_metadata,
)
from services.utils import aon_url, rarity_counts


def _normalized_text(value: object) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


def live_view(live_token: str):
    """Redirect a stable player URL to its campaign's current snapshot."""
    try:
        state = live_channel(live_token)
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


def live_version(live_token: str):
    """Return the current snapshot version for persistent player polling."""
    try:
        state = live_channel(live_token)
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


def player_view():
    data = request.values
    try:
        channel = normalize_channel(data.get("channel"))
    except ValueError as exc:
        abort(400, str(exc))
    roll_id = str(data.get("roll_id") or "").strip()
    live_token = str(request.args.get("live") or "").strip().lower()
    if live_token:
        try:
            live_state = live_channel(live_token)
        except (LiveChannelNotFound, ValueError):
            return render_template("player_view_missing.html"), 404
        except (OSError, sqlite3.Error):
            abort(503, "Live Player View storage is temporarily unavailable.")
        if live_state["channel"] != channel:
            abort(404)

    if not roll_id:
        return redirect("/")

    try:
        snapshot = load_snapshot(roll_id, channel)
    except SnapshotNotFound:
        return render_template(
            "player_view_missing.html", channel=channel, roll_id=roll_id
        ), 404
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
        current_app.logger.exception("Unable to load Player View snapshot")
        abort(503, "Player View storage is temporarily unavailable.")

    raw_lists: dict = {}
    meta: dict = {}
    if isinstance(snapshot, dict) and snapshot:
        if "lists" in snapshot or "shop" in snapshot:
            raw_lists = snapshot.get("lists") or {}
            meta = snapshot.get("shop") or {}
        else:
            # Continue to display snapshots created before the nested format.
            raw_lists = legacy_snapshot_lists(snapshot)
            meta = {
                "shop_type": snapshot.get("shop_type"),
                "shop_size": snapshot.get("shop_size"),
                "disposition": snapshot.get("disposition"),
                "party_level": snapshot.get("party_level"),
                "seed": snapshot.get("seed"),
                "window": snapshot.get("window"),
            }

    if not raw_lists:
        abort(400, "Player View snapshot is missing or invalid.")

    lists = player_visible_lists(raw_lists)

    shop_name = _normalized_text(meta.get("shop_name") or meta.get("name")) or None
    default_label = (_normalized_text(meta.get("shop_type")) or "Shop").title()
    return render_template(
        "results_player.html",
        page_title=f"Player View — {shop_name or default_label}",
        shop_name=shop_name,
        shop_type=meta.get("shop_type"),
        seed=meta.get("seed"),
        aon_url=aon_url,
        channel=channel,
        roll_id=roll_id,
        live_token=live_token,
        **template_inventory_context(lists),
    )


def results_view(roll_id: str):
    """Render a stored GM result without repeating the generation request."""
    try:
        channel = normalize_channel(request.args.get("channel"))
    except ValueError as exc:
        abort(400, str(exc))
    try:
        snapshot = load_snapshot(roll_id, channel)
        live_state = channel_state(channel)
    except (SnapshotNotFound, LiveChannelNotFound):
        abort(404, "That generated shop is no longer available.")
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
        current_app.logger.exception("Unable to load generated shop result")
        abort(503, "Player View storage is temporarily unavailable.")

    meta = snapshot.get("shop") or {}
    lists = inventory_lists(snapshot.get("lists") or {})
    summary = snapshot.get("summary") or {}
    all_items = flattened_inventory(lists)
    counts = summary.get("counts") or rarity_counts(all_items)
    picked = summary.get("picked") or section_counts(lists)
    picked.setdefault("critical", count_critical(all_items))
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
        aon_url=aon_url,
        window=meta.get("window"),
        roll_id=roll_id,
        channel=channel,
        live_token=live_state["live_token"],
        is_live=live_state["current_token"] == roll_id,
        generation_request_key=secrets.token_urlsafe(24),
        curation=snapshot.get("curation") or {},
        inventory_sections=section_template_context(lists),
        **template_inventory_context(lists),
    )


def publish_player_view():
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


def history():
    channel = str(request.args.get("channel") or "").strip()
    archived = str(request.args.get("archived") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    try:
        page = int(request.args.get("page") or 1)
        if page < 1:
            raise ValueError("History page must be a positive whole number.")
        per_page = 50
        count_options = {"channel": channel or None}
        if archived:
            count_options["archived"] = True
        total_snapshots = snapshot_count(**count_options)
        page_count = max(1, (total_snapshots + per_page - 1) // per_page)
        page = min(page, page_count)
        recent_options = {
            "channel": channel or None,
            "limit": per_page,
            "offset": (page - 1) * per_page,
        }
        if archived:
            recent_options["archived"] = True
        snapshots = recent_snapshots(**recent_options)
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
        showing_archived=archived,
        channels=channels,
        total_snapshots=total_snapshots,
        page=page,
        page_count=page_count,
        first_result=((page - 1) * per_page + 1) if total_snapshots else 0,
        last_result=min(page * per_page, total_snapshots),
    )


def history_backup():
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    download_name = f"pf2e-player-views-{timestamp}.db"
    try:
        with tempfile.TemporaryDirectory(prefix="pf2e-player-view-backup-") as directory:
            backup_path = backup_database(Path(directory) / download_name)
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


def history_update_metadata():
    try:
        channel = normalize_channel(request.form.get("channel"))
        token = str(request.form.get("roll_id") or "").strip()
        update_snapshot_metadata(
            token,
            channel,
            shop_name=request.form.get("shop_name") or "",
            settlement=request.form.get("settlement") or "",
        )
    except ValueError as exc:
        abort(400, str(exc))
    except SnapshotNotFound:
        abort(404, "That stored shop no longer exists.")
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Unable to update shop archive metadata")
        abort(503, "Shop archive storage is temporarily unavailable.")
    return redirect(url_for("history", channel=channel, updated=token))


def history_archive():
    try:
        channel = normalize_channel(request.form.get("channel"))
        token = str(request.form.get("roll_id") or "").strip()
        archived = str(request.form.get("archived") or "1") == "1"
        set_snapshot_archived(token, channel, archived)
    except ValueError as exc:
        abort(400, str(exc))
    except SnapshotNotFound:
        abort(404, "That stored shop no longer exists.")
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Unable to update shop archive state")
        abort(503, "Shop archive storage is temporarily unavailable.")
    return redirect(url_for("history", channel=channel, archived="1" if archived else None))


def history_delete():
    try:
        channel = normalize_channel(request.form.get("channel"))
        token = str(request.form.get("roll_id") or "").strip()
        if str(request.form.get("confirmation") or "").strip().upper() != "DELETE":
            raise ValueError("Enter DELETE to permanently remove this draft.")
        delete_snapshot(token, channel)
    except ValueError as exc:
        abort(400, str(exc))
    except SnapshotNotFound:
        abort(404, "That stored shop no longer exists.")
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Unable to delete stored shop")
        abort(503, "Shop archive storage is temporarily unavailable.")
    return redirect(url_for("history", channel=channel, deleted="1"))


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


def register_routes(app: Flask) -> None:
    """Register routes with their established endpoint names."""
    app.add_url_rule("/live/<live_token>", "live_view", live_view, methods=["GET"])
    app.add_url_rule(
        "/api/live/<live_token>/version", "live_version", live_version, methods=["GET"]
    )
    app.add_url_rule("/player-view", "player_view", player_view, methods=["GET"])
    app.add_url_rule("/results/<roll_id>", "results_view", results_view, methods=["GET"])
    app.add_url_rule(
        "/player-view/publish",
        "publish_player_view",
        publish_player_view,
        methods=["POST"],
    )
    app.add_url_rule("/history", "history", history, methods=["GET"])
    app.add_url_rule("/history/backup", "history_backup", history_backup, methods=["POST"])
    app.add_url_rule(
        "/history/make-live", "history_make_live", history_make_live, methods=["POST"]
    )
    app.add_url_rule(
        "/history/metadata",
        "history_update_metadata",
        history_update_metadata,
        methods=["POST"],
    )
    app.add_url_rule("/history/archive", "history_archive", history_archive, methods=["POST"])
    app.add_url_rule("/history/delete", "history_delete", history_delete, methods=["POST"])
    app.add_url_rule(
        "/history/rotate-live",
        "history_rotate_live",
        history_rotate_live,
        methods=["POST"],
    )
