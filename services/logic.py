# services/logic.py
from dataclasses import dataclass
from typing import Dict, Tuple
import logging, re
from typing import Callable

import pandas as pd
from services.utils import (
    to_gp,
    normalize_str_columns,
    apply_adjustments_probabilistic,
    apply_materials_probabilistic,
)
from services.db import load_formula_rows, load_items
from services.catalog_order import canonicalize_frame
from services.settings import CONFIG
from services.randomness import get_rng
from services.money import cp_to_gp, format_gp, gp_to_cp, multiply_cp
from services.spell_items import (
    enrich_magic_wands as _enrich_magic_wands_impl,
    enrich_spell_scrolls as _enrich_spell_scrolls_impl,
    load_spell_cache as _load_spell_cache,
    parse_scroll_level as _parse_scroll_level,
    parse_wand_rank as _parse_wand_rank,
)
GROUPS = CONFIG.get("source_table_groups", {})
logger = logging.getLogger(__name__)

_ST_ALIAS_MAP = {
    "held_items": "held_item",
    "held_item": "held_item",
    "ccstructure": "cc_structure",
    "c_c_structure": "cc_structure",
    "cc_structure": "cc_structure",
    "specific_magic_shield": "specific_magic_shield",
    "specific_magic_shields": "specific_magic_shield",
}


def _normalize_source_table_token(value: str) -> str:
    """Normalize a source_table identifier to config-style tokens."""
    token = str(value or "").strip().lower()
    token = re.sub(r"[\s\-]+", "_", token)
    token = re.sub(r"[^a-z0-9_]+", "", token)
    token = re.sub(r"_+", "_", token).strip("_")
    return _ST_ALIAS_MAP.get(token, token)
    
def _format_price(gp_value: float | None) -> str:
    """Format a gp float into PF2e denominations (gp/sp/cp)."""
    return format_gp(gp_value)


def _multiply_gp(gp_value: float, multiplier: float) -> float:
    """Apply a multiplier with a single, deterministic copper-piece rounding step."""
    return float(cp_to_gp(multiply_cp(gp_to_cp(gp_value), multiplier)))


def _group(key: str, default: list[str]) -> list[str]:
    vals = GROUPS.get(key)
    return vals if isinstance(vals, list) and vals else default


@dataclass
class PickCounts:
    mundane: int
    armor: int
    weapons: int
    magic: int

# -------- helpers --------

def _level_window(party_level: int) -> Tuple[int, int]:
    caps   = CONFIG.get("level_caps", {"min": 1, "max": 20})
    spread = CONFIG.get("level_spread", {"min_offset": -3, "max_offset": 1})
    lo = max(caps["min"], min(caps["max"], party_level + spread["min_offset"]))
    hi = max(caps["min"], min(caps["max"], party_level + spread["max_offset"]))
    if hi < lo: hi = lo
    return lo, hi

def _normalize_pair(v, default=(0, 0)) -> tuple[int, int]:
    if isinstance(v, (list, tuple)) and len(v) == 2:
        lo, hi = int(v[0]), int(v[1])
    else:
        lo, hi = default
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi

def _counts_block(shop_type: str, shop_size: str) -> dict:
    """
    Return the band block for this shop_type+size, falling back to size-only CONFIG['counts'].
    """
    st = (shop_type or "").strip().lower()
    sz = (shop_size or "medium").strip().lower()
    by_shop = (CONFIG.get("counts_by_shop") or {})
    # exact shop_type + size
    block = (by_shop.get(st) or {}).get(sz)
    if not block:
        # fallback: size-only counts
        block = (CONFIG.get("counts") or {}).get(sz, {})
    return block or {}

def _counts_for_size(shop_type: str, shop_size: str) -> PickCounts:
    block = _counts_block(shop_type, shop_size)
    r = get_rng()
    m_lo, m_hi = _normalize_pair(block.get("mundane"))
    a_lo, a_hi = _normalize_pair(block.get("armor"))
    w_lo, w_hi = _normalize_pair(block.get("weapons"))
    g_lo, g_hi = _normalize_pair(block.get("magic"))
    return PickCounts(
        mundane=r.randint(m_lo, m_hi),
        armor=r.randint(a_lo, a_hi),
        weapons=r.randint(w_lo, w_hi),
        magic=r.randint(g_lo, g_hi),
    )

def _counts_for_size_type(shop_type: str, shop_size: str, item_type: str) -> int:
    block = _counts_block(shop_type, shop_size)  # uses counts_by_shop if present, else counts
    band = block.get((item_type or "").strip().lower(), [0, 0])
    lo, hi = _normalize_pair(band)
    return get_rng().randint(lo, hi)

def _counts_for_specific_magic(shop_type_or_size: str | None = None,
                               maybe_shop_size: str | None = None) -> int:
    if maybe_shop_size is None:
        # legacy call: only size provided
        sz = (shop_type_or_size or "medium").strip().lower()
        band = (CONFIG.get("specific_magic_counts") or {}).get(sz, [0, 0])
    else:
        st = (shop_type_or_size or "").strip().lower()
        sz = (maybe_shop_size or "medium").strip().lower()
        by_shop = (CONFIG.get("specific_magic_counts_by_shop") or {})
        band = (by_shop.get(st) or {}).get(sz)
        if band is None:
            band = (CONFIG.get("specific_magic_counts") or {}).get(sz, [0, 0])

    lo, hi = _normalize_pair(band)
    return get_rng().randint(lo, hi)

def _filter_source_tables(df: pd.DataFrame, source_tables) -> pd.DataFrame:
    # Missing source metadata cannot be filtered safely.
    if "source_table" not in df.columns:
        logger.warning("Catalog is missing the required source_table column")
        return df.iloc[0:0]

    # Normalize requested tables
    if isinstance(source_tables, str):
        source_tables = [source_tables]

    wanted = {_normalize_source_table_token(s) for s in source_tables if str(s).strip()}
    if not wanted:
        logger.warning("No source tables were configured for this selection")
        return df.iloc[0:0]
    col_norm = df["source_table"].astype(str).map(_normalize_source_table_token)

    # Aliases are normalized above; selection itself is deliberately exact.
    mask = col_norm.isin(wanted)

    if not mask.any():
        available = sorted(set(col_norm.dropna().tolist()))[:25]
        logger.warning(
            "No catalog rows matched requested source tables %s; available sample=%s",
            sorted(wanted),
            available,
        )
        return df.iloc[0:0]
    return df[mask]

# --- shop type matching (exact + fuzzy) ---

def _normalize_shop(s: str) -> str:
    s = str(s or "").lower().strip()
    return "".join(ch for ch in s if ch.isalnum())

def _apply_shop_type(pool: pd.DataFrame, shop_type: str, strict: bool = False) -> pd.DataFrame:
    """
    Filter by shop_type with graceful fallbacks:
      1) exact (normalized) match
      2) alias map from CONFIG['shop_type_aliases']
      3) substring / startswith matches
      4) fuzzy (difflib) with configurable threshold
    Set strict=True to force exact-only (previous behavior).
    """
    if not shop_type or "shop_type" not in pool.columns or pool.empty:
        return pool

    # prepare
    from difflib import SequenceMatcher
    threshold = float(CONFIG.get("shop_type_fuzzy_threshold", 0.84))
    aliases   = CONFIG.get("shop_type_aliases", {})

    target_raw = str(shop_type).strip()
    target_norm = _normalize_shop(target_raw)

    col = pool["shop_type"].dropna().astype(str).map(str.strip)
    vals = col.unique().tolist()
    vals_norm = {_normalize_shop(v): v for v in vals}

    # 1) exact normalized match
    if target_norm in vals_norm:
        chosen = vals_norm[target_norm]
        return pool[col.str.lower() == chosen.lower()]

    if strict:
        # In strict mode, no exact match means no items for this shop.
        # This prevents unrelated categories from leaking into specialized shops
        # (e.g., Tattooist showing mundane/weapons/armor).
        return pool.iloc[0:0]

    # 2) alias map
    alias_hit = None
    if aliases:
        aliases_norm = {_normalize_shop(k): v for k, v in aliases.items() if v}
        if target_norm in aliases_norm:
            alias_hit = aliases_norm[target_norm]
            alias_norm = _normalize_shop(alias_hit)
            if alias_norm in vals_norm:
                chosen = vals_norm[alias_norm]
                return pool[col.str.lower() == chosen.lower()]

    # 3) substring / startswith
    starts = [v for v in vals if _normalize_shop(v).startswith(target_norm)]
    if len(starts) == 1:
        return pool[col.str.lower() == starts[0].lower()]
    contains = [v for v in vals if target_norm in _normalize_shop(v)]
    if len(contains) == 1:
        return pool[col.str.lower() == contains[0].lower()]

    # 4) fuzzy
    def score(a, b): return SequenceMatcher(None, a, b).ratio()
    best = None
    best_sc = 0.0
    for v in vals:
        sc = score(target_norm, _normalize_shop(v))
        if sc > best_sc:
            best_sc, best = sc, v

    if best and best_sc >= threshold:
        return pool[col.str.lower() == best.lower()]

    return pool

def _apply_shop_type_exact(pool: pd.DataFrame, shop_type: str) -> pd.DataFrame:
    return _apply_shop_type(pool, shop_type, strict=True)


def _level_bounds_for(item_type: str, party_level: int) -> tuple[int, int]:
    it = (item_type or "").lower()
    if it in ("mundane", "weapons", "armor", "materials"):
        return (0, party_level + 1)

    caps   = CONFIG.get("level_caps", {"min": 1, "max": 20})
    spread = CONFIG.get("level_spread", {"min_offset": -3, "max_offset": 1})
    lo = max(caps["min"], min(caps["max"], party_level + spread["min_offset"]))
    hi = max(caps["min"], min(caps["max"], party_level + spread["max_offset"]))
    if hi < lo: hi = lo
    return (lo, hi)


def _aggregate_items(rows_base, rows_crit, disposition: str) -> list[dict]:
    """
    Combine base + critical rows, collapse duplicates by (name, critical),
    sum quantities, and apply disposition to per-unit price. Robust to bad inputs.
    """
    # --- defensive normalization ---
    def _normalize_rows(rows):
        try:
            import pandas as _pd
        except Exception:
            _pd = None

        if isinstance(rows, dict):
            return [rows]
        if _pd is not None and isinstance(rows, _pd.DataFrame):
            return rows.to_dict(orient="records")
        if _pd is not None and isinstance(rows, _pd.Series):
            return [rows.to_dict()]
        if isinstance(rows, list):
            out = []
            for r in rows:
                if isinstance(r, dict):
                    out.append(r)
                elif _pd is not None and isinstance(r, _pd.Series):
                    out.append(r.to_dict())
            return out
        return []

    rows_base = _normalize_rows(rows_base)
    rows_crit = _normalize_rows(rows_crit)

    from collections import defaultdict
    bucket: dict[tuple[str, bool], dict] = {}
    qtys = defaultdict(int)
    crit_flags = defaultdict(bool)

    def _unit_price_text(r: dict) -> str:
        gp = to_gp(r.get("price_text", ""))
        if gp is None:
            return str(r.get("price_text", "")).strip()
        adj = _apply_disposition(gp, disposition)
        return _format_price(adj)

    def _add(r: dict, is_crit: bool):
        name = (r.get("name") or "").strip()
        if not name:
            return

        # Carry Source and detect 3PP on a per-row basis
        src = (r.get("Source") or r.get("source") or "").strip()
        pub = (r.get("Publisher_Source") or r.get("publisher_source") or "").strip().lower()
        is_3pp = pub in ("3rd party", "3rd-party", "third party", "3pp")

        # Use custom dedupe key if present
        key_name = str(r.get("_dedupe_key") or name).strip()
        key = (key_name, bool(is_crit))
        qtys[key] += 1
        crit_flags[key] = crit_flags[key] or bool(is_crit)

        if key not in bucket:
            bucket[key] = dict(r)  # keep originals as a base
            bucket[key].update({
                "name": name,
                "level": int(r.get("level", 0) or 0),
                "rarity": str(r.get("rarity", "")).strip().title(),
                "price": _unit_price_text(r),
                "quantity": 0,
                "category": r.get("category", ""),
                "critical": bool(is_crit),
            })
            # Persist Source and 3PP flag on the display dict
            if src:
                bucket[key]["Source"] = src
            if is_3pp:
                bucket[key]["is_3pp"] = True
                
    for r in rows_base:
        _add(r, False)
    for r in rows_crit:
        _add(r, True)

    items: list[dict] = []
    for key, it in bucket.items():
        it["quantity"] = qtys[key]
        it["critical"] = crit_flags[key]
        items.append(it)
    return items

def _ritual_display_name(base_name: str, level: int) -> str:
    """
    Format ritual names like a scroll: 'Ritual - X Level (Ritual Name)'.
    Avoid double-wrapping if it's already formatted.
    """
    bn = (base_name or "").strip()
    if bn.lower().startswith("ritual -"):
        return bn
    return f"Ritual - {int(level)} Level ({bn})"

def _boost_quantities(items: list[dict], shop_size: str, item_type: str) -> list[dict]:
    rules_all = CONFIG.get("quantity_boost", {}) or {}
    key = (item_type or "").lower()
    r = rules_all.get(key)
    if r is None and key == "armor":
        r = rules_all.get("weapons")
    if r:
        r = r.get((shop_size or "medium").strip().lower(), r.get("medium", {}))
    if not r or not items:
        return items

    p            = float(r.get("p", 0.55))
    add_min      = int(r.get("add_min", 0))
    add_max      = int(r.get("add_max", 2))
    crit_add_min = int(r.get("crit_add_min", add_min))
    crit_add_max = int(r.get("crit_add_max", add_max))
    max_per_item = int(r.get("max_per_item", 5))

    rng = get_rng()
    out = []
    for it in items:
        q = int(it.get("quantity", 1) or 1)

        # 🚫 Skip boosting uncommon/rare
        rarity = (it.get("rarity") or "Common").strip().title()
        if rarity in ("Uncommon", "Rare"):
            new_it = dict(it); new_it["quantity"] = q
            out.append(new_it)
            continue

        if rng.random() < p:
            if it.get("critical", False):
                q += rng.randint(crit_add_min, crit_add_max)
            else:
                q += rng.randint(add_min, add_max)

        q = max(1, min(q, max_per_item))
        new_it = dict(it); new_it["quantity"] = q
        out.append(new_it)

    return out




_ensure_spells_cache = _load_spell_cache

def _enrich_spell_scrolls(items: list[dict]) -> list[dict]:
    return _enrich_spell_scrolls_impl(
        items, spell_cache_loader=_ensure_spells_cache
    )


def _enrich_magic_wands(items: list[dict]) -> list[dict]:
    return _enrich_magic_wands_impl(items, spell_cache_loader=_ensure_spells_cache)
    





# Compatibility exports: callers keep importing these established names from
# services.logic while the implementation now lives in the rune domain module.
from services.rune_selection import (
    armor_fundamental_candidates as _fundamental_candidates_armor,
    armor_fundamental_property_candidates as _armor_fundamental_property_candidates,
    armor_property_candidates as _property_candidates_armor,
    format_fundamental_pair_label as _format_fundamental_pair_label,
    format_potency_label as _format_potency_label,
    is_shield as _is_shield,
    is_weapon_fundamental_property as _is_weapon_fundamental_property,
    load_runes_frame as _load_runes_df,
    pick_best_fundamental_property as _pick_best_fundamental_property,
    resolve_fundamental_property_rate as _resolve_fundamental_property_rate,
    resolve_rarity_weights as _resolve_rarity_weights,
    shield_property_candidates as _shield_property_candidates,
    weapon_fundamental_candidates as _fundamental_candidates,
    weapon_fundamental_property_candidates as _weapon_fundamental_property_candidates,
    weapon_property_candidates as _property_candidates,
    weighted_pick_by_rarity as _weighted_pick_by_rarity,
    weighted_pick_fundamental as _weighted_pick_fundamental,
)


def _select_items_core(
    df: pd.DataFrame,
    source_tables,
    item_type: str,
    shop_type: str,
    party_level: int,
    shop_size: str,
    disposition: str,
    include_crit: bool = True,
    count_override: int | None = None,   # NEW
):
    if df is None or df.empty:
        return [], [], {"base_count": 0, "critical_added": 0, "window": (0, 0)}

    d = normalize_str_columns(df, [
        "category", "source_table", "name", "rarity", "price_text",
        "tags", "shop_type", "Bulk", "Source", "subtype", "Publisher_Source"
    ])
    d = _filter_source_tables(d, source_tables)
    d = _apply_shop_type_exact(d, shop_type)

    # Exclude Unique items from all results
    if "rarity" in d.columns:
        d = d[~d["rarity"].str.strip().str.lower().eq("unique")]

    lo, hi = _level_bounds_for(item_type, party_level)
    if "level" in d.columns:
        if item_type.lower() in ("mundane", "weapons", "armor"):
            d = d[(d["level"] <= hi)]
        else:
            d = d[(d["level"] >= lo) & (d["level"] <= hi)]

    d = canonicalize_frame(d)

    if d.empty:
        return [], [], {"base_count": 0, "critical_added": 0, "window": (lo, hi)}

    # --- base count: allow override (for specific magic) ---
    base_n = (
        int(count_override)
        if count_override is not None
        else _counts_for_size_type(shop_type, shop_size, item_type.lower())
    )

    crit_pool = d[(d.get("stock_flag", 0) == 2)]
    norm_pool = d[(d.get("stock_flag", 0) != 2)]

    rng = get_rng()

    # --- rarity-weighted sampling for BASE picks ---
    def _rarity_weight_series(df_in: pd.DataFrame):
        rw = CONFIG.get("rarity_weights", {"Common": 90, "Uncommon": 9, "Rare": 1})

        r = df_in.get("rarity")
        if r is None:
            w = pd.Series([rw.get("Common", 1.0)] * len(df_in), index=df_in.index, dtype=float)
        else:
            r_norm = r.astype(str).str.strip().str.title()
            w = r_norm.map(lambda x: float(rw.get(x, rw.get("Common", 1.0))))
            common_w = float(rw.get("Common", 1.0))
            w = w.fillna(common_w).clip(lower=0.0)

        if not (w > 0).any():
            w = pd.Series([1.0] * len(df_in), index=df_in.index, dtype=float)
        return w

    # Base (norm_pool) sampling
    base_rows = pd.DataFrame()
    if base_n > 0 and not norm_pool.empty:
        if item_type.lower() == "magic" and isinstance(source_tables, (list, tuple, set)):
            # --- Uniform-per-source sampling so each source_table has equal chance ---
            npool = norm_pool.copy()

            want = [_normalize_source_table_token(s) for s in list(source_tables)]
            npool["_st_norm"] = npool["source_table"].astype(str).map(_normalize_source_table_token)

            # groups only for requested sources (ignore extras)
            groups = {st: npool[npool["_st_norm"] == st] for st in set(want)}
            k = len(want) if len(want) > 0 else 1

            # even quotas across sources, remainder distributed randomly
            order = list(want)
            rng.shuffle(order)
            q, r = divmod(base_n, k)
            quotas = {st: q + (1 if i < r else 0) for i, st in enumerate(order)}

            picks = []
            deficit = 0
            for st, take_n in quotas.items():
                g = groups.get(st)
                if take_n <= 0 or g is None or g.empty:
                    deficit += take_n
                    continue
                w = _rarity_weight_series(g)
                replace_needed = len(g) < take_n
                picks.append(g.sample(n=take_n, replace=replace_needed, weights=w,
                                      random_state=rng.randint(0, 10**9)))

            base_rows = pd.concat(picks, ignore_index=False) if picks else pd.DataFrame()

            # If some source_tables were empty, fill the deficit from the whole pool
            deficit += base_n - len(base_rows)
            if deficit > 0:
                w_all = _rarity_weight_series(npool)
                extra = npool.sample(n=deficit, replace=(len(npool) < deficit), weights=w_all,
                                     random_state=rng.randint(0, 10**9))
                base_rows = pd.concat([base_rows, extra], ignore_index=False)

            # cleanup temp column
            try:
                base_rows = base_rows.drop(columns=["_st_norm"])
            except Exception:
                pass

        else:
            # --- original behavior for non-magic types ---
            weights = _rarity_weight_series(norm_pool)
            base_rows = norm_pool.sample(
                n=base_n,
                replace=True,
                weights=weights,
                random_state=rng.randint(0, 10**9)
            )

    # --- critical pool unchanged ---
    crit_rows = []
    critical_added = 0
    if include_crit and not crit_pool.empty:
        rate = CONFIG.get("critical_bonus_rate", 0.25)
        target = max(1 if not crit_pool.empty else 0, int(round(base_n * rate)))
        if target > 0:
            take = min(target, max(len(crit_pool), target))
            crit_rows = crit_pool.sample(
                n=take,
                replace=True,
                random_state=rng.randint(0, 10**9)
            ).to_dict(orient="records")
            critical_added = len(crit_rows)

    items_pre = _aggregate_items(base_rows, crit_rows, disposition)

    # --- Capture base name once (do NOT compose here) ---
    if item_type.lower() == "weapons":
        for it in items_pre:
            it.setdefault("_base_name", (it.get("name", "") or "").strip())

    # ---------- Enrich spell scrolls ----------
    if item_type.lower() in ("magic", "scrolls"):
        items_pre = _enrich_spell_scrolls(items_pre)
    if item_type.lower() == "magic":
        items_pre = _enrich_magic_wands(items_pre)
        
    # --- adjustments for armor & weapons ---
    if item_type.lower() in ("armor", "weapons"):
        from services.db import load_adjustments
        adj_df = load_adjustments()
        if adj_df is not None and not adj_df.empty:
            adj_cfg   = CONFIG.get("adjustments", {}) or {}
            apply_map = adj_cfg.get("apply_rate", {}) or {}
            rar_w     = adj_cfg.get("rarity_weights", CONFIG.get("rarity_weights", {"Common":90,"Uncommon":9,"Rare":1}))
            name_tpl  = adj_cfg.get("name_template", "{adj} {base}")
            items_pre = apply_adjustments_probabilistic(
                items=items_pre,
                adjustments_df=adj_df,
                apply_rate_map=apply_map,
                rarity_weights=rar_w,
                name_template=name_tpl,
                rng=rng,
            )

    # --- materials for armor & weapons ---
    if item_type.lower() in ("armor", "weapons"):
        from services.db import load_materials
        material_types = ["weapon"] if item_type.lower() == "weapons" else ["armor", "shield"]
        materials_df = load_materials(material_types)
        if materials_df is not None and not materials_df.empty:
            mat_cfg = CONFIG.get("materials", {})
            apply_rate = float(mat_cfg.get("apply_rate", 0.05))
            name_tpl = mat_cfg.get("name_template", "{base} ({material})")
            items_pre = apply_materials_probabilistic(
                items=items_pre,
                materials_df=materials_df,
                apply_rate=apply_rate,
                party_level=party_level,
                name_template=name_tpl,
                rng=rng,
            )

    # --- runes: weapons ---
    if item_type.lower() == "weapons":
        runes_df = _load_runes_df()
        rune_cfg = (CONFIG.get("weapon_runes") or CONFIG.get("runes") or {})
        items_pre = [
            apply_weapon_runes(
                w, player_level=party_level, runes_df=runes_df, rng=rng, rune_cfg=rune_cfg
            )
            for w in items_pre
        ]
        fund_ct  = sum(1 for w in items_pre if w.get("_rune_fund_label"))
        props_ct = sum(1 for w in items_pre if w.get("_rune_prop_labels"))
        logger.debug(
            "Weapon runes applied: fundamentals=%d properties=%d items=%d",
            fund_ct,
            props_ct,
            len(items_pre),
        )

        # Compose final weapon names once all systems have annotated
        for it in items_pre:
            it["name"] = _compose_weapon_name(it)

    # --- runes: armor & shields ---
    if item_type.lower() == "armor":
        runes_df = _load_runes_df()
        # Prefer armor_runes / shield_runes configs when provided
        armor_cfg  = (CONFIG.get("armor_runes")  or CONFIG.get("runes") or {})
        shield_cfg = (CONFIG.get("shield_runes") or CONFIG.get("armor_runes") or CONFIG.get("runes") or {})

        new_list = []
        for a in items_pre:
            if _is_shield(a):
                new_list.append(
                    apply_shield_runes(
                        a, player_level=party_level, runes_df=runes_df, rng=rng, rune_cfg=shield_cfg
                    )
                )
            else:
                new_list.append(
                    apply_armor_runes(
                        a, player_level=party_level, runes_df=runes_df, rng=rng, rune_cfg=armor_cfg
                    )
                )
        items_pre = new_list

        # (Optional) compose armor (and shield) names to show rune label prefixes
        for it in items_pre:
            it["name"] = _compose_armor_name(it)

    # --- AoN scroll target cleanup (safe even if none present) ---
    _scroll_with_spell = re.compile(r"^Spell scroll \(\d+(?:st|nd|rd|th) level\)\s*-\s*(.+)$", re.IGNORECASE)
    for it in items_pre:
        nm = str(it.get("name", ""))
        m = _scroll_with_spell.match(nm)
        if m:
            it["aon_target"] = m.group(1).strip()

    # --- boost quantities and return triple ---
    items_post = _boost_quantities(items_pre, shop_size, item_type)

    return items_pre, items_post, {
        "base_count": base_n,
        "critical_added": critical_added,
        "window": (lo, hi),
        "pool_counts": {"norm": int(len(norm_pool)), "crit": int(len(crit_pool))},
    }








# Compatibility exports for established imports from services.logic. The
# implementation now lives in the runed-equipment domain module.
from services.runed_equipment import (
    apply_armor_runes,
    apply_shield_runes,
    apply_weapon_runes,
    compose_armor_name as _compose_armor_name,
    compose_weapon_name as _compose_weapon_name,
    extract_material_label_from_name as _extract_material_label_from_name,
    format_rune_display_name as _format_rune_display_name,
)


def _apply_disposition(gp: float, disposition: str) -> float:
    mults = CONFIG.get("disposition_multipliers", {"standard": 1.0, "fair": 1.0})
    m = mults.get((disposition or "fair").lower(), 1.0)
    return _multiply_gp(gp, m)


def select_items_by_source(
    df: pd.DataFrame,
    source_tables,
    item_type: str,
    shop_type: str,
    party_level: int,
    shop_size: str,
    disposition: str,
    include_crit: bool = True,
    count_override: int | None = None,   # NEW
) -> dict:
    items_pre, items_post, meta = _select_items_core(
        df=df,
        source_tables=source_tables,
        item_type=item_type,
        shop_type=shop_type,
        party_level=party_level,
        shop_size=shop_size,
        disposition=disposition,
        include_crit=include_crit,
        count_override=count_override,    # NEW
    )
    
    # --- Ritual display-name tweak: 'Ritual (1st level) - <Name>'
    import re

    def _ordinal(n: int) -> str:
        n = int(n)
        if 10 <= (n % 100) <= 13:
            suf = "th"
        else:
            suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{suf}"

    def _ritual_display_name(n: str, lvl: int) -> str:
        n = (n or "").strip()
        # If already in the desired shape, leave it
        if re.match(r"^\s*Ritual\s*\(\d+(st|nd|rd|th)\s+level\)\s*-\s*", n, re.I):
            return n
        # Convert any older 'Ritual - X Level (Name)' to the new shape, otherwise build from base
        m = re.match(r"^\s*Ritual\s*-\s*(\d+)\s*Level\s*\((.+)\)\s*$", n, re.I)
        if m:
            lvl = int(m.group(1))
            base = m.group(2).strip()
            return f"Ritual ({_ordinal(lvl)} level) - {base}"
        return f"Ritual ({_ordinal(lvl)} level) - {n}"

    for it in items_post:
        cat  = str(it.get("category") or "").lower()
        st   = str(it.get("source_table") or "").lower()
        tg_v = it.get("tags")
        tags = " ".join(tg_v) if isinstance(tg_v, list) else str(tg_v or "")
        if ("ritual" in cat) or ("ritual" in st) or ("ritual" in tags.lower()):
            lvl = int(it.get("level") or 0)
            base = str(it.get("name") or "").strip()
            it["name"] = _ritual_display_name(base, lvl)
            # keep link targeting the underlying ritual page if you carry a base target
            it["aon_target"] = it.get("aon_target") or base

        # Standalone runes should advertise that they're runes
        if (cat == "rune") or (st == "runes"):
            base_name = str(it.get("name") or "").strip()
            it["name"] = _format_rune_display_name(base_name)
            if not it.get("aon_target"):
                it["aon_target"] = base_name

    return {
        "items": items_post,
        "base_count": meta["base_count"],
        "critical_added": meta["critical_added"],
        "window": meta["window"],
    }


def select_mundane_items(df, shop_type, party_level, shop_size, disposition):
    return select_items_by_source(
        df=df,
        source_tables=_group("mundane", ["mundane"]),
        item_type="mundane",
        shop_type=shop_type, party_level=party_level,
        shop_size=shop_size, disposition=disposition, include_crit=True
    )

def select_weapons_items(df, shop_type, party_level, shop_size, disposition):
    return select_items_by_source(
        df=df,
        source_tables=_group("weapons", ["weapon_basic", "Weapon_Basic", "weapon", "weapons"]),
        item_type="weapons",
        shop_type=shop_type, party_level=party_level,
        shop_size=shop_size, disposition=disposition, include_crit=True
    )

def select_armor_items(df, shop_type, party_level, shop_size, disposition):
    return select_items_by_source(
        df=df,
        source_tables=_group("armor", ["armor_basic", "armors", "armor", "shield_basic", "shields", "shield"]),
        item_type="armor",
        shop_type=shop_type, party_level=party_level,
        shop_size=shop_size, disposition=disposition, include_crit=True
    )

def select_specific_magic_armor(df, shop_type, party_level, shop_size, disposition):
    return select_items_by_source(
        df=df,
        source_tables=_group("specific_magic_armor", ["specific_magic_armor", "specific_magic_shield"]),
        item_type="magic",
        shop_type=shop_type, party_level=party_level,
        shop_size=shop_size, disposition=disposition,
        include_crit=True,
        count_override=_counts_for_specific_magic(shop_type, shop_size),  # CHANGED
    )

def select_specific_magic_weapons(df, shop_type, party_level, shop_size, disposition):
    return select_items_by_source(
        df=df,
        source_tables=_group("specific_magic_weapons", ["specific_magic_weapons"]),
        item_type="magic",
        shop_type=shop_type, party_level=party_level,
        shop_size=shop_size, disposition=disposition,
        include_crit=True,
        count_override=_counts_for_specific_magic(shop_type, shop_size),  # CHANGED
    )

def select_magic_items(df, shop_type, party_level, shop_size, disposition):
    base = select_items_by_source(
        df=df,
        source_tables=_group("magic", [
        "alchemical_items", "cc_structure", "consumables", "grimoire",
        "held_item", "rune", "snares", "spellhearts", "staff_wand", "worn_items"
        ]),
        item_type="magic",
        shop_type=shop_type, party_level=party_level,
        shop_size=shop_size, disposition=disposition, include_crit=True
    )

    # Optional additive scroll picks: if counts[shop_size]["scrolls"] (or counts_by_shop)
    # is configured, these are added on top of the base magic count.
    scroll_extra_n = _counts_for_size_type(shop_type, shop_size, "scrolls")
    if scroll_extra_n <= 0:
        return base

    extra = select_items_by_source(
        df=df,
        source_tables=_group("scrolls", ["scrolls"]),
        item_type="magic",
        shop_type=shop_type,
        party_level=party_level,
        shop_size=shop_size,
        disposition=disposition,
        include_crit=True,
        count_override=scroll_extra_n,
    )

    out = dict(base or {})
    out["items"] = (base.get("items") or []) + (extra.get("items") or [])
    out["base_count"] = int(base.get("base_count") or 0) + int(extra.get("base_count") or 0)
    out["critical_added"] = int(base.get("critical_added") or 0) + int(extra.get("critical_added") or 0)
    return out
    
def select_materials(df, shop_type, party_level, shop_size, disposition):
    return select_items_by_source(
        df=df,
        source_tables=_group("materials", ["materials"]),
        item_type="materials", 
        shop_type=shop_type, party_level=party_level,
        shop_size=shop_size, disposition=disposition, include_crit=True
    )
    
def select_formula_items(df, shop_type, party_level, shop_size, disposition):
    return select_formulas(df, shop_type, party_level, shop_size, disposition)

    
# ---------- FORMULAS (new) ----------

def _formula_cost_table_default() -> dict[int, int]:
    """
    Fallback mapping of formula level -> gp cost, based on the provided table.
    """
    return {
        1: 1, 2: 2, 3: 3, 4: 5, 5: 8, 6: 13, 7: 18, 8: 25, 9: 35,
        10: 50, 11: 70, 12: 100, 13: 150, 14: 225, 15: 325, 16: 500,
        17: 750, 18: 1200, 19: 2000, 20: 3500,
    }

def _load_formula_costs_from_sqlite() -> dict[int, int]:
    """
    Load 'Formula' level->gp mapping from SQLite table 'Formula'.
    Tolerates schemas with (Level|ItemLevel) and (Price|PriceText).
    Falls back to the default table on any failure.
    """
    try:
        df = load_formula_rows()
        if df is None or df.empty:
            return _formula_cost_table_default()

        # find reasonable columns
        cols = {c.lower(): c for c in df.columns}
        lvl_col = cols.get("level") or cols.get("itemlevel")
        price_col = cols.get("price") or cols.get("pricetext")

        if not lvl_col:
            # try to derive from Name like "Formula - 7"
            if "Name" in df.columns:
                df["__level_guess"] = df["Name"].astype(str).str.extract(r"Formula\s*-\s*(\d+)", expand=False)
                df["__level_guess"] = pd.to_numeric(df["__level_guess"], errors="coerce").astype("Int64")
                lvl_col = "__level_guess"
            else:
                return _formula_cost_table_default()

        def _gp_parse(v):
            s = str(v or "").strip().lower()
            if not s:
                return None
            try:
                return int(float(s))
            except Exception:
                if s.endswith("gp"):
                    try: return int(float(s[:-2].strip()))
                    except Exception: return None
                return None

        df = df.copy()
        df[lvl_col] = pd.to_numeric(df[lvl_col], errors="coerce").astype("Int64")
        if price_col:
            df["__gp"] = df[price_col].map(_gp_parse)
        else:
            # heuristic last resort: any column hinting price/cost/gp
            num_cols = [c for c in df.columns if re.search(r"price|cost|gp", c, re.I)]
            df["__gp"] = df[num_cols[0]].map(_gp_parse) if num_cols else None

        out = {}
        for _, row in df.iterrows():
            lv = row.get(lvl_col)
            gp = row.get("__gp")
            if pd.notna(lv) and int(lv) > 0:
                val = None
                try:
                    if gp is not None and not pd.isna(gp):
                        val = int(gp)
                except Exception:
                    val = None
                out[int(lv)] = val if val is not None else _formula_cost_table_default().get(int(lv))

        base = _formula_cost_table_default()
        base.update(out)   # prefer db values
        return base
    except Exception:
        logger.warning("Could not load Formula prices; using built-in defaults", exc_info=True)
        return _formula_cost_table_default()

def _counts_for_formulas(shop_type: str, shop_size: str) -> int:
    """
    Prefer counts_by_shop[shop_type][shop_size]['formulas'] if present.
    Else fall back to counts[shop_size]['formulas'].
    If still missing, fall back to ...['materials'].
    Finally, default to [0,0].
    """
    st = (shop_type or "").strip().lower()
    sz = (shop_size or "").strip().lower()

    # Try per-shop override first
    shop_block = (CONFIG.get("counts_by_shop", {}).get(st, {}) or {}).get(sz, {}) or {}
    global_block = (CONFIG.get("counts", {}) or {}).get(sz, {}) or {}

    band = (
        shop_block.get("formulas")
        or global_block.get("formulas")
        or shop_block.get("materials")
        or global_block.get("materials")
        or [0, 0]
    )

    lo, hi = _normalize_pair(band, default=(0, 0))
    n = get_rng().randint(lo, hi) if hi >= lo else 0

    logger.debug('Formula count: shop="%s" size="%s" band=%s count=%d', st, sz, band, n)
    return n

def select_formulas(df: pd.DataFrame, shop_type: str, party_level: int, shop_size: str, disposition: str):
    """
    Build a list of formula entries derived from eligible items up to (party_level + 1).
    - Eligible sources: alchemical_items, cc_structure, consumables, grimoire, held_items,
                        runes, snares, spellhearts, staff_wand, worn_items,
                        specific_magic_armor, specific_magic_shield, specific_magic_weapons
    - Exclude Unique items
    - Formula Level = item level; Formula Rarity = item rarity
    - Formula Price = Formula table by Level (gp)
    - Name = 'Formula - <Level> (<Item Name>)'
    - Category = 'Formula'
    - No item_boosted (keep quantity=1)
    """
    if df is None or df.empty:
        return {"items": [], "base_count": 0, "critical_added": 0, "window": (0, 0)}

    eligible_sources = [
        "alchemical_items", "cc_structure", "consumables", "grimoire",
        "held_items", "runes", "snares", "spellhearts", "staff_wand",
        "worn_items", "specific_magic_armor", "specific_magic_shield",
        "specific_magic_weapons",
    ]

    # Normalize columns we might use
    d = normalize_str_columns(
        df,
        ["category","source_table","name","rarity","price_text","tags","shop_type","Bulk","Source","subtype"]
    )

    # --- IMPORTANT: normalize source_table names before filtering ---
    eligible_norm = { _normalize_source_table_token(x) for x in eligible_sources }
    if "source_table" in d.columns:
        d["__st"] = d["source_table"].apply(_normalize_source_table_token)
        d = d[d["__st"].isin(eligible_norm)]
    else:
        d = d.iloc[0:0]  # no source table info -> nothing eligible

    # Shop filter
    d = _apply_shop_type_exact(d, shop_type)

    # Exclude Unique
    if "rarity" in d.columns:
        d = d[~d["rarity"].str.strip().str.lower().eq("unique")]

    # Level window (<= party_level + 1, and >= 1)
    hi = int(party_level) + 1
    if "level" in d.columns:
        d["level"] = pd.to_numeric(d["level"], errors="coerce").fillna(0).astype(int)
        d = d[(d["level"] >= 1) & (d["level"] <= hi)]
    d = canonicalize_frame(d)

    # Count to pick
    base_n = _counts_for_formulas(shop_type, shop_size)

    logger.debug("Formula selection: pool=%d count=%d max_level=%d", len(d), base_n, hi)

    if d.empty or base_n <= 0:
        return {"items": [], "base_count": 0, "critical_added": 0, "window": (1, hi)}

    # Rarity-weighted sampling
    def _rarity_w_series(df_in: pd.DataFrame):
        rw = CONFIG.get("rarity_weights", {"Common": 80, "Uncommon": 16, "Rare": 4})
        r = df_in.get("rarity")
        if r is None:
            return pd.Series([rw.get("Common", 1.0)] * len(df_in), index=df_in.index, dtype=float)
        rr = r.astype(str).str.strip().str.title()
        w = rr.map(lambda x: float(rw.get(x, rw.get("Common", 1.0)))).fillna(float(rw.get("Common", 1.0)))
        if not (w > 0).any():
            w[:] = 1.0
        return w

    rng = get_rng()
    replace_needed = len(d) < base_n
    picks = d.sample(
        n=base_n,
        replace=replace_needed,
        weights=_rarity_w_series(d),
        random_state=rng.randint(0, 10**9),
    )

    # Price map (DB table if present, else fallback)
    lvl_cost = _load_formula_costs_from_sqlite()

    items = []
    for _, row in picks.iterrows():
        lvl = int(row.get("level") or 0)
        rarity = str(row.get("rarity") or "Common").strip().title()
        base_name_raw = str(row.get("name") or "").strip()
        st = str(row.get("source_table") or "").strip().lower()
        cat = str(row.get("category") or "").strip().lower()
        is_rune = (cat == "rune") or (st == "runes")
        base_name = _format_rune_display_name(base_name_raw) if is_rune else base_name_raw
        gp = lvl_cost.get(lvl, _formula_cost_table_default().get(lvl, 0))

        name = f"Formula - {lvl} ({base_name})"
        price_text = f"{gp} gp" if gp else "0 gp"

        items.append({
            "name": name,
            "level": lvl,
            "rarity": rarity,
            # keep raw and display (display goes through disposition as you had it)
            "price_text": price_text,
            "price": _format_price(_apply_disposition(gp, disposition)),
            "quantity": 1,
            "category": "Formula",
            "aon_target": base_name_raw,
            "critical": False,
        })

    return {"items": items, "base_count": base_n, "critical_added": 0, "window": (1, hi)}
