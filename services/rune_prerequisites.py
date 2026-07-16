"""Rune prerequisite parsing and equipment-context matching."""

from __future__ import annotations

import re


_DAMAGE_TYPE_TOKENS = {
    "acid",
    "bludgeoning",
    "cold",
    "electricity",
    "fire",
    "force",
    "negative",
    "piercing",
    "poison",
    "positive",
    "slashing",
    "sonic",
}
_USAGE_TOKENS = {"melee", "ranged", "thrown", "unarmed"}
_ARMOR_CATEGORY_TOKENS = {"light", "medium", "heavy"}
_MATERIAL_TOKENS = {
    "adamantine",
    "bone",
    "cold iron",
    "coldiron",
    "darkwood",
    "leather",
    "metal",
    "mithral",
    "nonmetal",
    "nonmetallic",
    "nonmetalic",
    "steel",
    "wood",
    "wooden",
}


def normalize_phrase(value: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_compact(value: str | None) -> str:
    return re.sub(r"\s+", "", normalize_phrase(value))


def tokenize_field(value) -> set[str]:
    tokens: set[str] = set()
    if value is None:
        return tokens
    iterable = value if isinstance(value, (list, tuple, set)) else [value]
    for raw in iterable:
        if raw is None:
            continue
        text = str(raw)
        for part in [text, *re.split(r"[;,/]+", text)]:
            normalized = normalize_phrase(part)
            if not normalized:
                continue
            tokens.add(normalized)
            tokens.add(normalize_compact(part))
            tokens.update(token for token in normalized.split(" ") if token)
    return {token for token in tokens if token}


def collect_item_context(item: dict) -> dict[str, set[str]]:
    fields = (
        "type",
        "Type",
        "subtype",
        "Subtype",
        "tags",
        "Tags",
        "traits",
        "Traits",
        "damage_type",
        "DamageType",
        "damage_types",
        "DamageTypes",
        "name",
        "Name",
        "category",
        "Category",
    )
    tokens: set[str] = set()
    for field in fields:
        tokens.update(tokenize_field(item.get(field)))
    materials = {token for token in tokens if token in _MATERIAL_TOKENS}
    if "coldiron" in tokens:
        materials.add("cold iron")
    return {
        "all": tokens,
        "usage": {token for token in tokens if token in _USAGE_TOKENS},
        "damage": {token for token in tokens if token in _DAMAGE_TYPE_TOKENS},
        "armor_category": {
            token for token in tokens if token in _ARMOR_CATEGORY_TOKENS
        },
        "materials": materials,
    }


def _categorize_prerequisite(token: str) -> str:
    if token in _ARMOR_CATEGORY_TOKENS:
        return "armor_category"
    if token in _USAGE_TOKENS:
        return "usage"
    if token in _DAMAGE_TYPE_TOKENS:
        return "damage_type"
    if token in _MATERIAL_TOKENS:
        return "material"
    if token == "shield":
        return "shield"
    return f"literal:{token}"


def extract_prerequisite_requirements(row: dict) -> dict[str, set[str]]:
    text = row.get("Prerequisite") or row.get("prerequisite") or ""
    if not str(text).strip():
        return {}
    requirements: dict[str, set[str]] = {}
    for chunk in re.split(r"[;,]", str(text).replace("/", ";")):
        for part in re.split(r"\bor\b", chunk.strip(), flags=re.IGNORECASE):
            normalized = normalize_phrase(part)
            if normalized:
                category = _categorize_prerequisite(normalized)
                requirements.setdefault(category, set()).add(normalized)
    return requirements


def prerequisites_match(row: dict, item_context: dict[str, set[str]]) -> bool:
    requirements = extract_prerequisite_requirements(row)
    if not requirements:
        return True
    all_tokens = item_context.get("all", set())
    for category, required in requirements.items():
        if category == "armor_category":
            if not required & item_context.get("armor_category", set()):
                return False
        elif category == "damage_type":
            damage = item_context.get("damage", set())
            if not damage or not required.issubset(damage):
                return False
        elif category == "usage":
            if not required & item_context.get("usage", set()):
                return False
        elif category == "material":
            if not required & item_context.get("materials", set()):
                return False
        elif category == "shield":
            if "shield" not in all_tokens:
                return False
        elif category.startswith("literal:"):
            token = next(iter(required), category.split(":", 1)[-1])
            if token not in all_tokens:
                return False
        elif not required & all_tokens:
            return False
    return True
