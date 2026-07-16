"""Magic Item Builder API routes."""
from __future__ import annotations

from copy import deepcopy
import random
import re

import pandas as pd
from flask import Blueprint, current_app, jsonify, request

from services.db import load_items
from services.logic import (
    CONFIG,
    GROUPS,
    _compose_armor_name,
    _compose_weapon_name,
    _load_runes_df,
    apply_armor_runes,
    apply_shield_runes,
    apply_weapon_runes,
)

bp = Blueprint("magic_builder", __name__)

_FUND_KEYS = {"fundamental", "fundamentals", "baseline"}
_PROP_KEYS = {
    "property",
    "properties",
    "property_runes",
    "weapon_properties",
    "armor_properties",
}
_FUND_LABELS = {"fundamental"}
_FUND_NAME_HINTS = {
    "weapon potency",
    "armor potency",
    "shield potency",
    "striking",
    "resilient",
    "reinforcing",
}


def _looks_fundamental_node(node: dict) -> bool:
    if not isinstance(node, dict):
        return False
    for key in ("group", "category", "type", "rune_type", "slot", "class"):
        if str(node.get(key) or "").strip().lower() in _FUND_LABELS:
            return True
    name = str(node.get("name") or "").strip().lower()
    return any(hint in name for hint in _FUND_NAME_HINTS)


def _force_fundamental_apply_rate_only(config, rate: float = 1.0) -> None:
    """Set only fundamental-rune application rates in a copied config tree."""
    if isinstance(config, dict):
        if "fundamental_apply_rate" in config:
            config["fundamental_apply_rate"] = rate
        for key, value in list(config.items()):
            normalized_key = str(key).strip().lower()
            if normalized_key in _FUND_KEYS:
                _force_fundamental_apply_rate_only(value, rate)
                if isinstance(value, dict) and "apply_rate" in value:
                    value["apply_rate"] = rate
                continue
            if normalized_key in _PROP_KEYS:
                continue
            if isinstance(value, dict):
                if _looks_fundamental_node(value) and "apply_rate" in value:
                    value["apply_rate"] = rate
                _force_fundamental_apply_rate_only(value, rate)
            elif isinstance(value, list):
                _force_fundamental_apply_rate_only(value, rate)
    elif isinstance(config, list):
        for value in config:
            if isinstance(value, dict) and _looks_fundamental_node(value):
                if "apply_rate" in value:
                    value["apply_rate"] = rate
            _force_fundamental_apply_rate_only(value, rate)


def _runes_config(item_type: str) -> dict:
    config = deepcopy(
        CONFIG.get(f"{item_type}_runes") or CONFIG.get("runes") or {}
    )
    _force_fundamental_apply_rate_only(config, 1.0)
    # The builder is explicitly creating a magic item. Avoid compounding the
    # normal shop-generation property gate with the per-slot roll: each weapon
    # or armor slot gets its configured chance directly. A shield has only its
    # single property-rune opportunity, so it always reaches that selection.
    property_config = config.get("property")
    if isinstance(property_config, dict):
        property_config["apply_rate"] = 1.0
    config["_prefer_higher_level"] = True
    return config


def _lower(value) -> str:
    return str(value or "").strip().lower()


def _st_norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _filter_by_sources(
    data: pd.DataFrame, preferred: list[str], fallback_group: str | None
) -> pd.DataFrame:
    data = data.copy()
    data["__st"] = data["source_table"].astype(str).map(_st_norm)
    wanted = {_st_norm(value) for value in preferred}
    output = data[data["__st"].isin(wanted)]
    if output.empty and fallback_group and fallback_group in GROUPS:
        alternatives = {_st_norm(value) for value in GROUPS.get(fallback_group, [])}
        output = data[data["__st"].isin(alternatives)]
    return output


def _base_pool(data: pd.DataFrame, item_type: str) -> pd.DataFrame:
    if item_type == "weapon":
        pool = _filter_by_sources(data, ["weapon_basic"], "weapons")
        return pool[pool["category_lc"].str.contains("weapon", na=False)]
    if item_type == "armor":
        pool = _filter_by_sources(data, ["armor_basic"], "armor")
        return pool[
            pool["category_lc"].str.contains("armor", na=False)
            & ~pool["category_lc"].str.contains("shield", na=False)
        ]
    fallback = "shields" if "shields" in GROUPS else "armor"
    pool = _filter_by_sources(data, ["shield_basic"], fallback)
    if not pool["source_table"].str.contains(
        "shield_basic", case=False, na=False
    ).any():
        is_shield = (
            pool["category_lc"].str.contains("shield", na=False)
            | pool["itype"].str.contains("shield", na=False)
            | pool["name"].str.lower().str.contains("shield", na=False)
        )
        pool = pool[is_shield]
    return pool


def _normalized_catalog() -> pd.DataFrame | None:
    data = load_items()
    if data is None or data.empty:
        return data
    data = data.copy()
    for column in (
        "name",
        "category",
        "type",
        "source_table",
        "level",
        "rarity",
        "price_text",
        "Bulk",
        "Source",
        "tags",
    ):
        if column not in data.columns:
            data[column] = ""
    data["name"] = data["name"].astype(str).str.strip()
    data["category_lc"] = data["category"].astype(str).str.strip().str.lower()
    data["itype"] = data["type"].astype(str).str.strip().str.lower()
    data["source_table"] = data["source_table"].astype(str).str.strip()
    data["level"] = pd.to_numeric(data["level"], errors="coerce").fillna(0).astype(int)
    return data


@bp.get("/api/magic-builder/bases")
def api_mib_bases():
    try:
        item_type = _lower(request.args.get("type"))
        subtype = _lower(request.args.get("subtype"))
        armor_type = _lower(request.args.get("armor_type"))
        if item_type not in ("weapon", "armor", "shield"):
            return jsonify(ok=False, error="Invalid type"), 400
        try:
            max_level = int(request.args.get("max_level") or 1)
        except (TypeError, ValueError):
            return jsonify(ok=False, error="max_level must be a whole number"), 400
        if not 1 <= max_level <= 20:
            return jsonify(ok=False, error="max_level must be between 1 and 20"), 400

        data = _normalized_catalog()
        if data is None or data.empty:
            current_app.logger.info("mib_bases: no data")
            return jsonify(ok=True, names=[])
        data = data[data["level"] <= max_level]
        pool = _base_pool(data, item_type)
        if item_type == "weapon" and subtype:
            matches = pool["itype"].eq(subtype)
            if not matches.any():
                tokens = set(re.findall(r"[a-z0-9]+", subtype))
                matches = pool["itype"].apply(
                    lambda value: tokens.issubset(set(re.findall(r"[a-z0-9]+", value)))
                )
            pool = pool[matches]
        elif item_type == "armor" and armor_type in ("light", "medium", "heavy"):
            matches = pool["itype"].eq(f"{armor_type} armor")
            if not matches.any():
                matches = pool["itype"].str.contains(
                    rf"\b{re.escape(armor_type)}\b", na=False
                )
            pool = pool[matches]

        names = sorted(pool["name"].dropna().unique().tolist())[:200]
        current_app.logger.info(
            "mib_bases: type=%s subtype=%s armor=%s max=%s -> %d names",
            item_type,
            subtype,
            armor_type,
            max_level,
            len(names),
        )
        return jsonify(ok=True, names=names)
    except Exception:
        current_app.logger.exception("mib_bases error")
        return jsonify(ok=False, error="Unable to load magic item bases"), 500


@bp.post("/api/magic-builder/build")
def api_mib_build():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(ok=False, error="JSON body must be an object"), 400
    item_type = _lower(data.get("item_type"))
    try:
        max_level = int(data.get("max_level") or 1)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="max_level must be a whole number"), 400
    base_name = str(data.get("base_name") or "").strip()
    if item_type not in ("weapon", "armor", "shield"):
        return jsonify(ok=False, error="Invalid item_type"), 400
    if not base_name:
        return jsonify(ok=False, error="Missing base_name"), 400
    if not 1 <= max_level <= 20:
        return jsonify(ok=False, error="max_level must be between 1 and 20"), 400
    if len(base_name) > 200 or any(ord(char) < 32 for char in base_name):
        return jsonify(ok=False, error="Invalid base_name"), 400

    catalog = _normalized_catalog()
    if catalog is None or catalog.empty:
        return jsonify(ok=False, error="No data loaded"), 500
    pool = _base_pool(catalog, item_type)
    candidates = pool[pool["name"].str.casefold() == base_name.casefold()]
    if candidates.empty:
        candidates = pool[
            pool["name"].str.lower().str.contains(base_name.lower(), na=False, regex=False)
        ]
    if candidates.empty:
        return jsonify(
            ok=False, error=f"Base '{base_name}' not found for type '{item_type}'"
        ), 404

    base = candidates.iloc[0]
    item = {
        "name": base["name"],
        "level": int(base["level"] or 0),
        "rarity": str(base["rarity"] or "Common").title(),
        "price_text": base.get("price_text") or "",
        "price": base.get("price_text") or "",
        "category": base.get("category")
        or ("Shield" if item_type == "shield" else item_type.title()),
        "type": base.get("type") or "",
        "Bulk": base.get("Bulk"),
        "Source": base.get("Source"),
        "tags": base.get("tags"),
        "_base_name": base["name"],
    }
    try:
        reroll = int(data.get("reroll") or 0)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="reroll must be a whole number"), 400
    if not 0 <= reroll <= 1_000_000:
        return jsonify(ok=False, error="reroll is outside the allowed range"), 400
    seed = f"{item_type}|{base['name']}|{max_level}"
    if reroll:
        seed += f"|{reroll}"
    rng = random.Random(seed)
    runes = _load_runes_df()
    rune_config = _runes_config(item_type)
    rune_config["_target_level"] = max_level
    if item_type == "weapon":
        item = apply_weapon_runes(
            item, player_level=max_level, runes_df=runes, rng=rng, rune_cfg=rune_config
        )
        composed = _compose_weapon_name(item)
    elif item_type == "armor":
        item = apply_armor_runes(
            item, player_level=max_level, runes_df=runes, rng=rng, rune_cfg=rune_config
        )
        composed = _compose_armor_name(item)
    else:
        item = apply_shield_runes(
            item, player_level=max_level, runes_df=runes, rng=rng, rune_cfg=rune_config
        )
        composed = _compose_armor_name(item)

    if not item.get("_rune_fund_label") and not item.get("_rune_prop_labels"):
        if item_type == "weapon":
            fundamental = "+3" if max_level >= 16 else "+2" if max_level >= 10 else "+1" if max_level >= 2 else None
            properties = ["Major Striking"] if max_level >= 19 else ["Greater Striking"] if max_level >= 12 else ["Striking"] if max_level >= 4 else []
        else:
            fundamental = "+3" if max_level >= 18 else "+2" if max_level >= 11 else "+1" if max_level >= 5 else None
            properties = ["Major Resilient"] if max_level >= 20 else ["Greater Resilient"] if max_level >= 14 else ["Resilient"] if max_level >= 8 else []
        if fundamental:
            item["_rune_fund_label"] = fundamental
        if properties:
            item["_rune_prop_labels"] = properties

    old_name = item.get("name") or base_name
    final_name = composed or old_name
    if final_name == old_name and item_type == "weapon":
        prefixes = []
        labels = item.get("_rune_prop_labels") or []
        prefixes.extend([label for label in labels if "striking" in label.lower()])
        prefixes.extend([label for label in labels if "striking" not in label.lower()])
        if item.get("_rune_fund_label"):
            prefixes.append(item["_rune_fund_label"])
        if prefixes:
            final_name = f"{' '.join(prefixes)} {final_name}"
    elif final_name == old_name:
        if item.get("_rune_prop_labels"):
            final_name = f"{item['_rune_prop_labels'][-1]} {final_name}"
        if item.get("_rune_fund_label"):
            final_name = f"{item['_rune_fund_label']} {final_name}"
    item["name"] = final_name
    if item.get("price") and not item.get("price_text"):
        item["price_text"] = item["price"]
    item["aon_target"] = item.get("_base_name") or item.get("name")
    return jsonify(ok=True, item=item)
