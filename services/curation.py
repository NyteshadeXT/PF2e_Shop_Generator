"""GM inventory-curation routes built on immutable snapshot revisions."""
from __future__ import annotations

from copy import deepcopy
import json
import sqlite3
import uuid

from flask import Blueprint, abort, current_app, jsonify, redirect, request, url_for

from services.db import load_items
from services.generation import generate_shop_snapshot, summarize_inventory
from services.player_views import (
    SnapshotNotFound,
    load_snapshot,
    normalize_channel,
    save_snapshot,
)


bp = Blueprint("curation", __name__)

SECTIONS = {
    "mundane": "mundane_items",
    "materials": "material_items",
    "formulas": "formula_items",
    "armor": "armor_items",
    "weapons": "weapon_items",
    "magic": "magic_items",
}


def _section(value: str) -> tuple[str, str]:
    name = str(value or "").strip().lower()
    if name not in SECTIONS:
        raise ValueError("Choose a valid inventory section.")
    return name, SECTIONS[name]


def _quantity(value) -> int:
    try:
        quantity = int(str(value or "1").strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Quantity must be a whole number.") from exc
    if not 1 <= quantity <= 999:
        raise ValueError("Quantity must be between 1 and 999.")
    return quantity


def _index(items: list, value) -> int:
    try:
        index = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Choose an item to edit.") from exc
    if not 0 <= index < len(items):
        raise ValueError("That item is no longer present in this draft.")
    return index


def _clean_text(value, label: str, maximum: int = 200) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required.")
    if len(text) > maximum or any(ord(char) < 32 for char in text):
        raise ValueError(f"{label} is too long or contains invalid characters.")
    return text


def _catalog_item(name: str, source_table: str | None = None) -> dict:
    catalog = load_items()
    if catalog is None or catalog.empty:
        raise ValueError("The item catalog is unavailable.")
    names = catalog["name"].astype(str).str.strip()
    matches = catalog[names.str.casefold().eq(name.strip().casefold())]
    if source_table and "source_table" in matches.columns:
        sources = matches["source_table"].astype(str).str.strip()
        matches = matches[sources.str.casefold().eq(source_table.strip().casefold())]
    if matches.empty:
        raise ValueError("That catalog item could not be found.")
    row = matches.iloc[0]

    def text_value(key: str, default: str = "") -> str:
        value = row.get(key)
        if value is None or str(value) in {"<NA>", "nan", "NaN"}:
            return default
        return str(value).strip()

    try:
        level = int(row.get("level"))
    except (TypeError, ValueError):
        level = 0
    item = {
        "name": text_value("name", name),
        "level": level,
        "rarity": text_value("rarity", "Common").title(),
        "price": text_value("price_text", text_value("price", "—")),
        "quantity": 1,
        "category": text_value("category", "Catalog Item"),
        "Source": text_value("Source"),
        "source_table": text_value("source_table"),
        "tags": text_value("tags"),
        "aon_target": text_value("name", name),
    }
    if item["source_table"].lower().startswith("specific_magic_"):
        item["is_magic_countable"] = True
    return item


def _custom_item(form) -> dict:
    try:
        level = int(str(form.get("level") or "0").strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Level must be a whole number.") from exc
    if not 0 <= level <= 30:
        raise ValueError("Level must be between 0 and 30.")
    rarity = str(form.get("rarity") or "Common").strip().title()
    if rarity not in {"Common", "Uncommon", "Rare", "Unique"}:
        raise ValueError("Choose a valid rarity.")
    return {
        "name": _clean_text(form.get("name"), "Item name"),
        "level": level,
        "rarity": rarity,
        "price": _clean_text(form.get("price") or "—", "Price", 50),
        "quantity": _quantity(form.get("quantity")),
        "category": _clean_text(form.get("category") or "Custom Item", "Category", 100),
        "Source": "Custom",
        "source_table": "custom",
        "is_magic_countable": str(form.get("magical") or "").lower() in {"1", "true", "on"},
    }


def _builder_item(raw: str) -> tuple[str, dict]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("The Magic Item Builder result is invalid.") from exc
    if not isinstance(payload, dict):
        raise ValueError("The Magic Item Builder result is invalid.")
    item_type = str(payload.pop("item_type", "")).strip().lower()
    section = "weapons" if item_type == "weapon" else "armor" if item_type in {"armor", "shield"} else "magic"
    item = {
        "name": _clean_text(payload.get("name"), "Item name"),
        "level": max(0, min(30, int(payload.get("level") or 0))),
        "rarity": str(payload.get("rarity") or "Common").strip().title(),
        "price": str(payload.get("price") or payload.get("price_text") or "—").strip(),
        "quantity": 1,
        "category": str(payload.get("category") or "Magic Item").strip(),
        "Source": str(payload.get("Source") or "Magic Item Builder").strip(),
        "aon_target": str(payload.get("aon_target") or payload.get("base_name") or payload.get("name") or "").strip(),
        "is_magic_countable": True,
    }
    return section, item


def _revised_snapshot(snapshot: dict, parent_token: str, operation: str) -> dict:
    revised = deepcopy(snapshot)
    curation = dict(revised.get("curation") or {})
    curation.update(
        {
            "is_curated": True,
            "revision": int(curation.get("revision") or 0) + 1,
            "parent_roll_id": parent_token,
            "last_operation": operation,
        }
    )
    revised["curation"] = curation
    revised["summary"] = summarize_inventory(revised.get("lists") or {})
    return revised


@bp.get("/api/catalog/search")
def catalog_search():
    query = str(request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify(ok=True, items=[])
    catalog = load_items()
    if catalog is None or catalog.empty:
        return jsonify(ok=True, items=[])
    mask = catalog["name"].astype(str).str.contains(query, case=False, regex=False, na=False)
    matches = catalog[mask].head(30)
    items = []
    for _, row in matches.iterrows():
        def safe_text(key: str, default: str = "") -> str:
            value = row.get(key)
            if value is None or str(value) in {"<NA>", "nan", "NaN"}:
                return default
            return str(value).strip()

        try:
            level = int(row.get("level"))
        except (TypeError, ValueError):
            level = 0
        items.append(
            {
                "name": safe_text("name"),
                "source_table": safe_text("source_table"),
                "level": level,
                "rarity": safe_text("rarity", "Common").title(),
                "category": safe_text("category"),
            }
        )
    return jsonify(ok=True, items=items)


@bp.post("/results/<roll_id>/curate")
def curate_snapshot(roll_id: str):
    try:
        channel = normalize_channel(request.form.get("channel"))
        snapshot = load_snapshot(roll_id, channel)
        operation = str(request.form.get("operation") or "").strip().lower()
        lists = snapshot.setdefault("lists", {})

        if operation in {"remove", "quantity", "hide", "reveal"}:
            _, list_key = _section(request.form.get("section"))
            items = lists.setdefault(list_key, [])
            index = _index(items, request.form.get("item_index"))
            if operation == "remove":
                items.pop(index)
            elif operation == "quantity":
                items[index]["quantity"] = _quantity(request.form.get("quantity"))
            else:
                items[index]["player_hidden"] = operation == "hide"
        elif operation == "add_catalog":
            _, list_key = _section(request.form.get("section"))
            item = _catalog_item(
                _clean_text(request.form.get("catalog_name"), "Catalog item"),
                str(request.form.get("catalog_source") or "").strip() or None,
            )
            item["quantity"] = _quantity(request.form.get("quantity"))
            lists.setdefault(list_key, []).append(item)
        elif operation == "add_custom":
            _, list_key = _section(request.form.get("section"))
            lists.setdefault(list_key, []).append(_custom_item(request.form))
        elif operation == "add_builder":
            section, item = _builder_item(request.form.get("item_json"))
            lists.setdefault(SECTIONS[section], []).append(item)
        elif operation == "rebuild":
            _, list_key = _section(request.form.get("section"))
            shop = snapshot.get("shop") or {}
            submitted = {
                "shop_type": shop.get("shop_type"),
                "shop_size": shop.get("shop_size"),
                "disposition": shop.get("disposition"),
                "shop_name": shop.get("shop_name"),
                "party_level": shop.get("party_level"),
                "seed": f"{shop.get('seed') or 'curated'}|{list_key}|{uuid.uuid4().hex}",
            }
            replacement = generate_shop_snapshot(load_items(), submitted)
            lists[list_key] = deepcopy((replacement.get("lists") or {}).get(list_key) or [])
        else:
            raise ValueError("Choose a valid curation action.")

        revised = _revised_snapshot(snapshot, roll_id, operation)
        new_token = uuid.uuid4().hex
        save_snapshot(new_token, channel, revised, advance_channel=False)
    except SnapshotNotFound:
        abort(404, "That shop draft no longer exists.")
    except (ValueError, TypeError) as exc:
        abort(400, str(exc))
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Unable to save curated shop revision")
        abort(503, "Shop storage is temporarily unavailable.")
    return redirect(url_for("results_view", channel=channel, roll_id=new_token), code=303)
