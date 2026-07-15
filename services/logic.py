# services/logic.py
from dataclasses import dataclass
from typing import Dict, List, Tuple
import logging, random, re
from typing import Callable

import pandas as pd
from services.utils import (
    to_gp,
    normalize_str_columns,
    apply_adjustments_probabilistic,
    apply_materials_probabilistic,
    bump_rarity,
    add_price,
    parse_potency_rank,
    within_range,
)
from services.db import load_formula_rows, load_items, load_spells
from services.catalog_order import canonicalize_frame
from services.settings import CONFIG
from services.randomness import get_rng
from services.money import cp_to_gp, format_gp, gp_to_cp, multiply_cp


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


# ---------- Name composition helpers (NEW) ----------

_MAT_LABEL_RX = re.compile(r"\(([^)]+)\)\s*$")
_RUNE_PREFIX_RX = re.compile(r"^\s*rune\s*[:\-]", re.IGNORECASE)

def _format_rune_display_name(name: str) -> str:
    """Prefix standalone rune names with "Rune:" unless already labeled."""
    base = str(name or "").strip()
    if not base:
        return base
    if _RUNE_PREFIX_RX.match(base):
        return base
    return f"Rune: {base}"

def _extract_material_label_from_name(name: str) -> str | None:
    """
    Best-effort: if a previous step templated materials as 'Base (Material [Grade])',
    recover the label from trailing parentheses.
    """
    m = _MAT_LABEL_RX.search((name or "").strip())
    if not m:
        return None
    return m.group(1).strip()

def _compose_weapon_name(it: dict) -> str:
    """
    Final name order:
      Fundamental Rune → Property Rune(s) → Material → Adjustment(s) → Base
    """
    base = (it.get("_base_name") or it.get("base_name") or it.get("name") or "").strip()

    parts: list[str] = []

    # Fundamental rune
    rf = (it.get("_rune_fund_label") or "").strip()
    if rf:
        parts.append(rf)

    # Property runes (use pick order; de-dupe)
    rp = it.get("_rune_prop_labels") or []
    rp = [r.strip() for r in rp if r and isinstance(r, str)]
    if rp:
        seen = set()
        rp_u = []
        for r in rp:
            k = r.lower()
            if k not in seen:
                seen.add(k)
                rp_u.append(r)
        parts.extend(rp_u)

    # Material (e.g., "Cold Iron (Low-Grade)")
    ml = str(it.get("_mat_label") or "").strip()
    if ml:
        parts.append(ml)

    # Adjustments: prefer explicit labels; otherwise parse existing tags (adjustment:<name>)
    adjs = list(it.get("_adj_labels") or [])
    if not adjs:
        tags = str(it.get("tags", ""))
        for tok in tags.split(","):
            tok = tok.strip()
            if tok.lower().startswith("adjustment:"):
                adjs.append(tok.split(":", 1)[1].strip())
    parts.extend([a for a in adjs if a])

    # Build → de-dupe whitespace
    import re as _re
    return _re.sub(r"\s+", " ", " ".join([*parts, base]).strip())

def _compose_armor_name(it: dict) -> str:
    """Rune (fundamental + properties) → Material → Adjustment(s) → Base."""
    base = (it.get("_base_name") or it.get("name") or "").strip()
    parts: list[str] = []
    rf = (it.get("_rune_fund_label") or "").strip()
    if rf: parts.append(rf)
    rp = it.get("_rune_prop_labels") or []
    rp = [r.strip() for r in rp if r and isinstance(r, str)]
    if rp:
        seen=set(); rp_u=[]
        for r in rp:
            k=r.lower()
            if k not in seen:
                seen.add(k); rp_u.append(r)
        parts.extend(rp_u)
    ml = (it.get("_mat_label") or "").strip()
    if ml: parts.append(ml)
    adjs = it.get("_adj_labels") or []
    if not adjs:
        tags = str(it.get("tags",""))
        for tok in tags.split(","):
            tok = tok.strip()
            if tok.lower().startswith("adjustment:"):
                adjs.append(tok.split(":",1)[1].strip())
    parts.extend([a for a in adjs if a])
    import re as _re
    return _re.sub(r"\s+"," "," ".join([*parts, base]).strip())


def _apply_adjustments_to_items(
    items: list[dict],
    adjustments_df: pd.DataFrame,
    item_type: str,
    party_level: int,
    disposition: str,
    rng: random.Random,
    respect_level_window: bool = True,
    skip_specific_magic: bool = True,
) -> list[dict]:
    """
    Legacy internal adjustment handler retained for compatibility with older flows.
    Not used in the main pipeline anymore; external services.utils.apply_adjustments_probabilistic is preferred.
    """
    cfg = CONFIG.get("adjustments", {}) or {}
    # Per-type rates (fallback by item_type if subtype-specific not present)
    base_rate = float(cfg.get("apply_rate", {}).get(item_type.lower(), 0.0))
    rar_w     = cfg.get("rarity_weights", CONFIG.get("rarity_weights", {"Common":90,"Uncommon":9,"Rare":1}))
    name_tpls = cfg.get("name_template", {})
    name_tpl  = name_tpls.get(item_type.lower(), "{base} ({adj})")

    if adjustments_df is None or adjustments_df.empty or not items:
        return items

    A = adjustments_df.copy()
    for c in ("name","subtype","rarity","price_text","level"):
        if c in A.columns:
            A[c] = A[c].astype(str).str.strip() if c != "level" else A[c]
    # Use magic-style window for adjustments themselves
    lo_adj, hi_adj = _level_bounds_for("magic", party_level)
    if "level" in A.columns:
        A = A[(A["level"].astype(int) >= lo_adj) & (A["level"].astype(int) <= hi_adj)]
    A = canonicalize_frame(A)
    if A.empty:
        return items

    # For optional window check on the fused result
    lo_out, hi_out = _level_bounds_for(item_type, party_level)

    def _subtype_of(it: dict) -> str | None:
        c = str(it.get("category","")).lower()
        st = str(it.get("source_table","")).lower()
        nm = str(it.get("name","")).lower()
        if "shield" in (c + st + nm): return "Shield"
        if "weapon" in (c + st):      return "Weapon"
        if "armor"  in (c + st):      return "Armor"
        return None

    def _rarity_max(rb: str, ra: str) -> str:
        order = {"Common":0,"Uncommon":1,"Rare":2,"Unique":3}
        RB, RA = (rb or "Common").title(), (ra or "Common").title()
        return RB if order.get(RB,0) >= order.get(RA,0) else RA

    def _pick_with_weights(df_pool: pd.DataFrame) -> pd.Series:
        wmap = {k.title(): float(v) for k, v in (rar_w or {}).items()}
        common_w = float(wmap.get("Common", 1.0))
        rar = df_pool["rarity"].astype(str).str.strip().str.title()
        ws  = rar.map(lambda x: wmap.get(x, common_w)).fillna(common_w).clip(lower=0.0)
        if not (ws > 0).any():
            ws[:] = 1.0
        return df_pool.sample(n=1, replace=True, weights=ws, random_state=rng.randint(0,10**9)).iloc[0]

    out: list[dict] = []
    for it in items:
        # Optional: don’t stack on specific-magic items
        if skip_specific_magic and str(it.get("category","")).lower().startswith("specific"):
            out.append(it); continue

        subtype = _subtype_of(it)
        if subtype not in ("Armor","Shield","Weapon"):
            out.append(it); continue

        # Allow subtype-specific rate overrides
        rate = float(cfg.get("apply_rate", {}).get(subtype.lower(), base_rate))
        if rng.random() >= rate:
            out.append(it); continue

        pool = A[A["subtype"].str.title().eq(subtype)]
        if pool.empty:
            out.append(it); continue

        pick = _pick_with_weights(pool)

        # ----- PRICE: base (already disposition-adjusted) + adjustment (apply same disposition) -----
        base_gp = to_gp(it.get("price", ""))
        adj_raw = to_gp(pick.get("price_text",""))
        # Note: base 'price' is already disposition-adjusted; we just add the adj gp.
        new_gp  = (base_gp or 0.0) + (adj_raw or 0.0)
        new_price_text = _format_price(new_gp)

        # ----- RARITY: take the rarer -----
        new_rarity = _rarity_max(it.get("rarity","Common"), pick.get("rarity","Common"))

        # ----- LEVEL: max(base, adjustment), optionally enforce window -----
        base_lvl = int(it.get("level", 0) or 0)
        adj_lvl  = int(pick.get("level", 0) or 0)
        fused_lv = max(base_lvl, adj_lvl)
        if respect_level_window and fused_lv > hi_out:
            out.append(it); continue  # skip applying this adj; keep base item

        # Compose the fused row
        fused = dict(it)
        # Important: don't finalize name here; add label only.
        fused.setdefault("_adj_labels", []).append(str(pick.get("name","")).strip())
        fused["rarity"] = new_rarity
        fused["level"]  = fused_lv
        fused["price"]  = new_price_text
        fused["tags"]   = ", ".join(x for x in [it.get("tags",""), f"adjustment:{pick.get('name','')}"] if x).strip()
        out.append(fused)

    return out


# ---------- NEW: Scroll enrichment ----------

_SCROLL_RE = re.compile(
    r"spell\s*scroll\s*\((\d+)(?:st|nd|rd|th)\s*level\)",
    re.IGNORECASE,
)
_WAND_LEVEL_RX = re.compile(r"(\d+)(?:st|nd|rd|th)[-\s]*(?:level|rank) spell", re.IGNORECASE)

_SPELLS_DF_CACHE: pd.DataFrame | None = None
_SPELLS_BY_RANK_CACHE: dict[int, pd.DataFrame] | None = None
_SPELLS_CACHE_SIGNATURE = None


def _ensure_spells_cache() -> tuple[pd.DataFrame, dict[int, pd.DataFrame]]:
    global _SPELLS_DF_CACHE, _SPELLS_BY_RANK_CACHE, _SPELLS_CACHE_SIGNATURE

    spells_df = pd.DataFrame()
    try:
        source = load_spells()
        signature = source.attrs.get("reference_signature")
        if (
            _SPELLS_DF_CACHE is not None
            and _SPELLS_BY_RANK_CACHE is not None
            and _SPELLS_CACHE_SIGNATURE == signature
        ):
            return _SPELLS_DF_CACHE, _SPELLS_BY_RANK_CACHE
        spells_df = source.rename(
            columns={"name": "Name", "rank": "Rank", "rarity": "Rarity", "source": "Source"}
        )[["Name", "Rank", "Rarity", "Source"]].copy()
    except Exception:
        logger.warning("Could not load the Spells table", exc_info=True)
        signature = None

    if spells_df is None or spells_df.empty:
        _SPELLS_DF_CACHE = pd.DataFrame()
        _SPELLS_BY_RANK_CACHE = {}
        _SPELLS_CACHE_SIGNATURE = signature
        return _SPELLS_DF_CACHE, _SPELLS_BY_RANK_CACHE

    if "Name" in spells_df.columns:
        spells_df["Name"] = spells_df["Name"].astype(str).str.strip()
    if "Rank" in spells_df.columns:
        spells_df["Rank"] = pd.to_numeric(spells_df["Rank"], errors="coerce").fillna(0).astype(int)
    else:
        spells_df["Rank"] = 0
    if "Rarity" in spells_df.columns:
        spells_df["Rarity"] = spells_df["Rarity"].astype(str).str.strip().str.title()
    else:
        spells_df["Rarity"] = "Common"
    if "Source" in spells_df.columns:
        spells_df["Source"] = spells_df["Source"].astype(str).str.strip()
    else:
        spells_df["Source"] = ""
    spells_df = canonicalize_frame(spells_df)

    by_rank: dict[int, pd.DataFrame] = {}
    if "Rank" in spells_df.columns:
        for rank, grp in spells_df.groupby("Rank"):
            by_rank[int(rank)] = grp.copy()

    _SPELLS_DF_CACHE = spells_df
    _SPELLS_BY_RANK_CACHE = by_rank
    _SPELLS_CACHE_SIGNATURE = signature
    return _SPELLS_DF_CACHE, _SPELLS_BY_RANK_CACHE

def _parse_scroll_level(name: str) -> int | None:
    raw = (name or "").strip()
    # Avoid re-parsing already enriched names like "Spell scroll (...) - Fireball"
    base = raw.split(" - ", 1)[0].strip()
    m = _SCROLL_RE.search(base)
    return int(m.group(1)) if m else None

def _rarity_multiplier_map() -> dict[str, float]:
    # Configurable; safe defaults if not present
    return {
        **{"Uncommon": 1.25, "Rare": 1.50},  # defaults
        **(CONFIG.get("rarity_price_multipliers", {}) or {})
    }

def _enrich_spell_scrolls(items: list[dict]) -> list[dict]:
    """
    For each picked item, if it's a Spell scroll (Nth level), choose a random spell
    from Spells where Rank = N. Append the spell name to the item's display name and
    bump price for Uncommon/Rare spells using config multipliers.
    """
    if not items:
        return items

    spells_df, spells_by_rank = _ensure_spells_cache()
    if spells_df is None or spells_df.empty:
        return items

    mults = _rarity_multiplier_map()

    rng = get_rng()
    expanded: list[dict] = []
    for it in items:
        qty = max(1, int(it.get("quantity") or 1))
        for _ in range(qty):
            unit = dict(it)
            unit["quantity"] = 1
            expanded.append(unit)

    out_units: list[dict] = []
    for it in expanded:
        name = str(it.get("name", "")).strip()
        if " - " in name:
            maybe_base = name.split(" - ", 1)[0].strip()
            if _SCROLL_RE.search(maybe_base):
                out_units.append(it)
                continue
        lvl = _parse_scroll_level(name)
        if lvl is None:
            out_units.append(it)
            continue

        pool = spells_by_rank.get(int(lvl)) if spells_by_rank else None
        if pool is None or pool.empty:
            out_units.append(it)
            continue

        pick = pool.sample(n=1, replace=True, random_state=rng.randint(0, 10**9)).iloc[0]
        spell_name = str(pick.get("Name", "")).strip()
        spell_rar  = str(pick.get("Rarity", "Common")).title()
        spell_src  = str(pick.get("Source", "")).strip()

        # Bump price using rarity multiplier (applied to the already disposition-adjusted 'price' text)
        base_gp = to_gp(it.get("price", ""))
        if base_gp is None:
            # try original price_text -> disposition was applied earlier; fallback to raw text if needed
            base_gp = to_gp(it.get("price_text", ""))
        new_gp = base_gp or 0.0
        mult   = float(mults.get(spell_rar, 1.0))
        new_gp = _multiply_gp(new_gp, mult)

        fused = dict(it)
        fused["name"]  = f"{name} - {spell_name}"
        fused["price"] = _format_price(new_gp)
        fused["spell"] = {"name": spell_name, "rarity": spell_rar, "rank": int(lvl)}
        fused["aon_target"] = spell_name
        if spell_src:
            fused["Source"] = spell_src
        out_units.append(fused)

    merged: dict[tuple, dict] = {}
    for it in out_units:
        key = (
            str(it.get("name", "")).strip(),
            str(it.get("price", "")).strip(),
            str(it.get("rarity", "")).strip(),
            int(it.get("level") or 0),
            bool(it.get("critical")),
        )
        if key not in merged:
            merged[key] = dict(it)
            merged[key]["quantity"] = 0
        merged[key]["quantity"] += int(it.get("quantity") or 1)

    return list(merged.values())

def _parse_wand_rank(name: str, level: int | None = None) -> int | None:
    m = _WAND_LEVEL_RX.search((name or "").strip())
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None

    if level is None:
        return None

    try:
        lvl = int(level)
    except Exception:
        return None

    if lvl <= 0:
        return None

    return max(1, min(10, (lvl + 1) // 2))


def _enrich_magic_wands(items: list[dict]) -> list[dict]:
    """Append spell details to magic and specialty wands using spell-rank criteria."""
    if not items:
        return items

    spells_df, spells_by_rank = _ensure_spells_cache()
    if spells_df is None or spells_df.empty:
        return items

    mults = _rarity_multiplier_map()
    rng = get_rng()
    out: list[dict] = []

    for it in items:
        item_type = str(it.get("type", "")).strip().lower()
        name = str(it.get("name", "")).strip()

        if item_type != "wands" or not name:
            out.append(it)
            continue

        lvl = None
        try:
            lvl = int(it.get("level") or 0)
        except Exception:
            lvl = None

        rank = _parse_wand_rank(name, lvl)
        if not rank:
            out.append(it)
            continue

        pool = spells_by_rank.get(int(rank)) if spells_by_rank else None
        if pool is None or pool.empty:
            out.append(it)
            continue

        pick = pool.sample(n=1, replace=True, random_state=rng.randint(0, 10**9)).iloc[0]
        spell_name = str(pick.get("Name", "")).strip()
        if not spell_name:
            out.append(it)
            continue
        spell_rar = str(pick.get("Rarity", "Common")).strip().title() or "Common"

        base_gp = to_gp(it.get("price", ""))
        if base_gp is None:
            base_gp = to_gp(it.get("price_text", ""))
        new_gp = _multiply_gp(base_gp or 0.0, float(mults.get(spell_rar, 1.0)))

        fused = dict(it)
        fused["name"] = f"{name} - {spell_name}"
        fused["price"] = _format_price(new_gp)
        fused["spell"] = {"name": spell_name, "rarity": spell_rar, "rank": int(rank)}
        fused["aon_target"] = spell_name
        out.append(fused)

    return out
    
def _is_shield(item: dict) -> bool:
    sub = (item.get("subtype") or item.get("Subtype") or "").strip().lower()
    cat = (item.get("category") or "").strip().lower()
    return ("shield" in sub) or ("shield" in cat)

def _is_shield_property(row: dict) -> bool:
    t = (row.get("Type") or row.get("type") or "").strip().lower()
    return t == "shield property runes"

_DAMAGE_TYPE_TOKENS = {
    "acid", "bludgeoning", "cold", "electricity", "fire", "force",
    "negative", "piercing", "poison", "positive", "slashing", "sonic",
}

_USAGE_TOKENS = {"melee", "ranged", "thrown", "unarmed"}
_ARMOR_CATEGORY_TOKENS = {"light", "medium", "heavy"}
_MATERIAL_TOKENS = {
    "adamantine", "bone", "cold iron", "coldiron", "darkwood", "leather",
    "metal", "mithral", "nonmetal", "nonmetallic", "nonmetalic",
    "steel", "wood", "wooden",
}


def _normalize_phrase(value: str | None) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _normalize_compact(value: str | None) -> str:
    return re.sub(r"\s+", "", _normalize_phrase(value))


def _tokenize_field(value) -> set[str]:
    tokens: set[str] = set()
    if value is None:
        return tokens
    if isinstance(value, (list, tuple, set)):
        iterable = value
    else:
        iterable = [value]
    for raw in iterable:
        if raw is None:
            continue
        text = str(raw)
        parts = [text]
        parts.extend(re.split(r"[;,/]+", text))
        for part in parts:
            norm = _normalize_phrase(part)
            if not norm:
                continue
            tokens.add(norm)
            tokens.add(_normalize_compact(part))
            tokens.update(p for p in norm.split(" ") if p)
    return {t for t in tokens if t}


def _collect_item_context(item: dict) -> dict[str, set[str]]:
    keys = (
        "type", "Type", "subtype", "Subtype", "tags", "Tags", "traits", "Traits",
        "damage_type", "DamageType", "damage_types", "DamageTypes",
        "name", "Name", "category", "Category",
    )
    tokens: set[str] = set()
    for key in keys:
        tokens.update(_tokenize_field(item.get(key)))
    usage = {t for t in tokens if t in _USAGE_TOKENS}
    damage = {t for t in tokens if t in _DAMAGE_TYPE_TOKENS}
    armor = {t for t in tokens if t in _ARMOR_CATEGORY_TOKENS}
    materials = {t for t in tokens if t in _MATERIAL_TOKENS}
    if any("coldiron" == t for t in tokens):
        materials.add("cold iron")
    return {
        "all": tokens,
        "usage": usage,
        "damage": damage,
        "armor_category": armor,
        "materials": materials,
    }


def _categorize_prereq_token(token: str) -> str:
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


def _extract_prereq_requirements(row: dict) -> dict[str, set[str]]:
    text = row.get("Prerequisite") or row.get("prerequisite") or ""
    if not text or not str(text).strip():
        return {}
    reqs: dict[str, set[str]] = {}
    cleaned = str(text).replace("/", ";")
    for chunk in re.split(r"[;,]", cleaned):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = re.split(r"\bor\b", chunk, flags=re.IGNORECASE)
        for part in parts:
            norm = _normalize_phrase(part)
            if not norm:
                continue
            category = _categorize_prereq_token(norm)
            reqs.setdefault(category, set()).add(norm)
    return reqs


def _prerequisites_match(row: dict, item_context: dict[str, set[str]]) -> bool:
    reqs = _extract_prereq_requirements(row)
    if not reqs:
        return True
    tokens_all = item_context.get("all", set())
    for category, required in reqs.items():
        if category == "armor_category":
            if not (required & item_context.get("armor_category", set())):
                return False
        elif category == "damage_type":
            damage_tokens = item_context.get("damage", set())
            if not damage_tokens or not required.issubset(damage_tokens):
                return False
        elif category == "usage":
            if not (required & item_context.get("usage", set())):
                return False
        elif category == "material":
            if not (required & item_context.get("materials", set())):
                return False
        elif category == "shield":
            if "shield" not in tokens_all:
                return False
        elif category.startswith("literal:"):
            token = next(iter(required), category.split(":", 1)[-1])
            if token not in tokens_all:
                return False
        else:
            if not (required & tokens_all):
                return False
    return True


def _shield_property_candidates(
    all_runes: list[dict], party_level: int, shield_row: dict
) -> list[dict]:
    hi = int(party_level) + 1
    context = _collect_item_context(shield_row)
    out = []
    for r in all_runes:
        if not _is_shield_property(r):
            continue
        if not _prerequisites_match(r, context):
            continue
        rl = int(r.get("level") or 0)
        if rl <= hi:
            out.append(r)
    return out

# ---------- Armor rune helpers ----------

def _is_armor_fundamental(row: dict) -> bool:
    t = (row.get("Type") or row.get("type") or "").strip().lower()
    st = (row.get("Subtype") or row.get("subtype") or "").strip().lower()
    n = (row.get("name") or "").strip().lower()
    # tolerant: any fundamental+armor labeling or classic names (potency/resilient)
    if "fundamental" in t and "armor" in t:
        return True
    if "fundamental" in st and "armor" in st:
        return True
    if ("armor" in t or "armor" in st or "armor" in n) and ("potency" in n or "resilient" in n):
        return True
    return False

def _is_armor_fundamental_property(row: dict) -> bool:
    if not _is_armor_fundamental(row):
        return False
    n = (row.get("name") or "").strip().lower()
    if "resilient" in n:
        return True
    t = (row.get("Type") or row.get("type") or "").strip().lower()
    return "fundamental" in t and "property" in t and "armor" in t

def _is_armor_property(row: dict) -> bool:
    t = (row.get("Type") or row.get("type") or "").strip().lower()
    return t == "armor property runes"

def _potency_cap_for_armor_level(pl: int) -> int:
    pl = int(pl or 0)
    if pl < 5:    return 0
    if pl < 11:   return 1   # 5..10
    if pl < 18:   return 2   # 11..17
    return 3                  # 18+

def _fundamental_candidates_armor(all_runes, armor_level, party_level):
    if int(party_level) < 5:
        return []
    cap = _potency_cap_for_armor_level(party_level)
    if cap <= 0:
        return []
    lvl_hi = int(party_level) + 1
    out = []
    for r in all_runes:
        if not _is_armor_fundamental(r):
            continue
        pr = parse_potency_rank(r.get("name"))
        if pr < 1 or pr > cap:
            continue
        if int(r.get("level") or 0) <= lvl_hi:
            out.append(r)
    return out

def _armor_fundamental_property_candidates(
    all_runes: list[dict], *, potency_rank: int, party_level: int
) -> list[dict]:
    return _collect_fundamental_property_candidates(
        all_runes,
        potency_rank=potency_rank,
        party_level=party_level,
        cap_func=_potency_cap_for_armor_level,
        predicate=_is_armor_fundamental_property,
    )
    
def _property_candidates_armor(
    all_runes: list[dict], party_level: int, armor_row: dict
) -> list[dict]:
    lo, hi = party_level - 3, party_level + 1
    context = _collect_item_context(armor_row)
    out = []
    for r in all_runes:
        if not _is_armor_property(r):
            continue
        if not _prerequisites_match(r, context):
            continue
        rl = int(r.get("level") or 0)
        if lo <= rl <= hi:
            out.append(r)
    return out

def _potency_cap_for_weapon_level(pl: int) -> int:
    pl = int(pl or 0)
    if pl < 2:   return 0
    if pl < 10:  return 1   # 2..9
    if pl < 16:  return 2   # 10..15
    return 3                 # 16+

def _is_fundamental(row: dict) -> bool:
    t = (row.get("Type") or row.get("type") or "").strip().lower()
    n = (row.get("name") or "").strip().lower()
    # tolerate pluralization and schema variance
    # e.g., "Weapon Fundamental Runes", "Weapon Fundamentals", etc.
    return (("fundamental" in t and "weapon" in t) or
            ("potency" in n and "weapon" in n))

def _is_property(row: dict) -> bool:
    t = (row.get("Type") or row.get("type") or "").strip().lower()
    return t == "weapon property runes"

def _is_weapon_fundamental_property(row: dict) -> bool:
    t = (row.get("Type") or row.get("type") or "").strip().lower()
    n = (row.get("name") or "").strip().lower()
    return ("weapon" in t and "fundamental" in t and "property" in t) or (
        "fundamental" in n and "property" in n and "weapon" in n
    )

def _required_potency_for_fundamental_property(name: str) -> int:
    n = (name or "").strip().lower()
    if not n:
        return 1
    if "major" in n:
        return 3
    if "greater" in n:
        return 2
    return 1

def _collect_fundamental_property_candidates(
    all_runes: list[dict],
    *,
    potency_rank: int,
    party_level: int,
    cap_func: Callable[[int], int],
    predicate: Callable[[dict], bool],
) -> list[dict]:
    potency_rank = int(potency_rank or 0)
    if potency_rank <= 0:
        return []
    cap = int(cap_func(party_level))
    if cap <= 0:
        return []
    lvl_hi = int(party_level) + 1
    ranked: list[tuple[int, int, dict]] = []
    for r in all_runes:
        if not predicate(r):
            continue
        required = _required_potency_for_fundamental_property(r.get("name"))
        if required > potency_rank or required > cap:
            continue
        level = int(r.get("level") or 0)
        if level > lvl_hi:
            continue
        ranked.append((required, level, r))
    ranked.sort(key=lambda tup: (tup[0], tup[1]), reverse=True)
    return [r for _, _, r in ranked]

def _weapon_fundamental_property_candidates(
    all_runes: list[dict], *, potency_rank: int, party_level: int
) -> list[dict]:
    return _collect_fundamental_property_candidates(
        all_runes,
        potency_rank=potency_rank,
        party_level=party_level,
        cap_func=_potency_cap_for_weapon_level,
        predicate=_is_weapon_fundamental_property,
    )

def _pick_best_fundamental_property(
    candidates: list[dict], potency_rank: int, rng: random.Random
) -> dict | None:
    if not candidates:
        return None
    best_required = max(
        _required_potency_for_fundamental_property(r.get("name"))
        for r in candidates
        if _required_potency_for_fundamental_property(r.get("name")) <= potency_rank
    ) if any(
        _required_potency_for_fundamental_property(r.get("name")) <= potency_rank
        for r in candidates
    ) else None
    if best_required is not None:
        pool = [
            r
            for r in candidates
            if _required_potency_for_fundamental_property(r.get("name")) == best_required
        ]
    else:
        pool = candidates
    return pool[rng.randint(0, len(pool) - 1)]

def _resolve_fundamental_property_rate(
    rune_cfg: dict | None,
    *,
    default: float = 0.6,
) -> float:
    """Read the fundamental-property apply rate with backward-compatible fallbacks."""

    def _as_float(value) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    if isinstance(rune_cfg, dict):
        # Preferred: dedicated "fundamental property" block.
        fp_cfg = rune_cfg.get("fundamental property") or rune_cfg.get("fundamental_property")
        if isinstance(fp_cfg, dict):
            rate = _as_float(fp_cfg.get("apply_rate"))
            if rate is not None:
                return rate

        # Legacy fallback: the property_pair_rate lived under the fundamental block.
        fund_cfg = rune_cfg.get("fundamental")
        if isinstance(fund_cfg, dict):
            rate = _as_float(fund_cfg.get("property_pair_rate"))
            if rate is not None:
                return rate

    return float(default)
    
def _resolve_rarity_weights(primary: dict | None, fallback: dict | None = None) -> dict[str, float]:
    """Merge rarity-weight mappings and normalize to usable floats."""
    merged: dict[str, float] = {}
    for source in (fallback, primary):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            try:
                weight = float(value)
            except (TypeError, ValueError):
                continue
            merged[str(key).strip().title()] = max(weight, 0.0)

    if not merged:
        merged = {"Common": 1.0}
    if merged.get("Common", 0.0) <= 0:
        merged["Common"] = 1.0
    return merged


def _weighted_pick_by_rarity(
    pool: list[dict], rng: random.Random, rarity_weights: dict[str, float]
) -> dict | None:
    if not pool:
        return None

    weights: list[float] = []
    default_weight = float(rarity_weights.get("Common", 1.0))
    for entry in pool:
        rarity = str(entry.get("rarity") or "Common").strip().title()
        weight = rarity_weights.get(rarity, default_weight)
        try:
            weight = float(weight)
        except (TypeError, ValueError):
            weight = default_weight
        weights.append(max(weight, 0.0))

    if not any(w > 0 for w in weights):
        return pool[rng.randint(0, len(pool) - 1)]

    total = sum(weights)
    pick_point = rng.random() * total
    acc = 0.0
    for entry, weight in zip(pool, weights):
        acc += weight
        if pick_point <= acc:
            return entry
    return pool[-1]

def _format_fundamental_pair_label(potency_rune: dict | None, property_rune: dict) -> str:
    prop_label = str(property_rune.get("name") or "").strip()
    if not potency_rune:
        return prop_label
    rank = parse_potency_rank(potency_rune.get("name"))
    if rank:
        potency_label = f"+{rank}"
    else:
        potency_label = str(potency_rune.get("name") or "").strip()
    return " ".join(part for part in (potency_label, prop_label) if part)
    
def _fundamental_candidates(all_runes, weapon_level, party_level):
    if int(party_level) < 2:
        return []
    cap = _potency_cap_for_weapon_level(party_level)
    if cap <= 0:
        return []
    lvl_hi = int(party_level) + 1
    out = []
    for r in all_runes:
        if not _is_fundamental(r):
            continue
        pr = parse_potency_rank(r.get("name"))
        level = int(r.get("level") or 0)
        if pr >= 1:
            if pr > cap:
                continue
            if level <= lvl_hi:
                out.append(r)
            continue
        if _is_weapon_fundamental_property(r) and level <= lvl_hi:
            required = _required_potency_for_fundamental_property(r.get("name"))
            if required <= cap:
                has_potency = any(
                    _is_fundamental(p)
                    and not _is_weapon_fundamental_property(p)
                    and required <= parse_potency_rank(p.get("name")) <= cap
                    and int(p.get("level") or 0) <= lvl_hi
                    for p in all_runes
                )
                if has_potency:
                    out.append(r)
    return out

def _weighted_pick_fundamental(cands: list[dict], rng: random.Random, cfg: dict | None) -> dict | None:
    if not cands:
        return None
    fcfg = (cfg or {}).get("fundamental", {}) if cfg else {}
    raw_weights = fcfg.get("potency_weights") if isinstance(fcfg, dict) else None

    if raw_weights:
        pot_w = {str(k): float(v) for k, v in raw_weights.items()}
    else:
        ranks = {
            parse_potency_rank(r.get("name"))
            for r in cands
            if parse_potency_rank(r.get("name")) > 0
        }
        if ranks:
            pot_w = {str(rank): float(rank) for rank in sorted(ranks)}
        else:
            pot_w = {}

    weights = []
    for r in cands:
        pr = parse_potency_rank(r.get("name"))
        w = float(pot_w.get(str(pr), 1.0))
        weights.append(max(w, 0.0001))

    # simple roulette-wheel using rng
    total = sum(weights)
    pick_point = rng.random() * total
    acc = 0.0
    for r, w in zip(cands, weights):
        acc += w
        if pick_point <= acc:
            return r
    return cands[-1]


def _property_candidates(
    all_runes: list[dict], party_level: int, weapon_row: dict
) -> list[dict]:
    lo, hi = party_level - 3, party_level + 1
    context = _collect_item_context(weapon_row)
    out = []
    for r in all_runes:
        if not _is_property(r):
            continue
        if not _prerequisites_match(r, context):
            continue
        rl = int(r.get("level") or 0)
        if lo <= rl <= hi:
            out.append(r)
    out.sort(key=lambda x: int(x.get("level") or 0), reverse=True)
    return out

def _load_runes_df() -> pd.DataFrame:
    df = load_items()
    if df is None or df.empty:
        return pd.DataFrame()
    R = df.copy()
    for c in ("source_table","Type","name","rarity","price_text"):
        if c in R.columns:
            R[c] = R[c].astype(str).str.strip()
    if "level" in R.columns:
        R["level"] = pd.to_numeric(R["level"], errors="coerce").fillna(0).astype(int)
    return canonicalize_frame(R[R.get("source_table","").str.lower().eq("runes")])


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


def apply_weapon_runes(
    weapon: dict,
    *,
    player_level: int,
    runes_df: pd.DataFrame,
    rng: random.Random,
    rune_cfg: dict | None = None
) -> dict:
    """
    Apply fundamentals/properties with probability knobs and constraints.
    Store labels for name composition; do not mutate the weapon name here.
    """
    fused = dict(weapon)
    fused.setdefault("_base_name", (weapon.get("name") or "").strip())

    # Early out if no runes table
    R = canonicalize_frame(pd.DataFrame(runes_df))
    if R.empty:
        fused["runes"] = []
        return fused

    # Normalize columns we rely on
    for c in ("Type", "name", "rarity", "price_text", "source_table", "level"):
        if c in R.columns:
            if c == "level":
                R[c] = pd.to_numeric(R[c], errors="coerce").fillna(0).astype(int)
            else:
                R[c] = R[c].astype(str).str.strip()

    # Keep only rune rows if source_table exists
    if "source_table" in R.columns:
        R = R[R["source_table"].str.lower().eq("runes")]

    all_runes = R.to_dict(orient="records")
    if not all_runes:
        fused["runes"] = []
        return fused

    # Config
    rcfg = (rune_cfg or {}) if rune_cfg is not None else (CONFIG.get("runes", {}) or {})
    fund_cfg = (rcfg.get("fundamental") or {}) if isinstance(rcfg, dict) else {}
    fund_rate = float(fund_cfg.get("apply_rate", 1.0))   # default: try fund always
    pair_rate = _resolve_fundamental_property_rate(rcfg)
    prop_cfg = (rcfg.get("property") or {}) if isinstance(rcfg, dict) else {}
    prop_rate = float(prop_cfg.get("apply_rate", 0.30))
    per_slot  = float(prop_cfg.get("per_slot_rate", 0.30))
    rarity_weights = _resolve_rarity_weights(
        primary=(rcfg.get("rarity_weights") if isinstance(rcfg, dict) else None),
        fallback=CONFIG.get("rarity_weights", {"Common": 1.0}),
    )


    # Base state from current weapon row
    weapon_level = int(fused.get("level") or 0)
    base_rarity  = (fused.get("rarity") or "Common").strip().title()
    base_gp      = to_gp(fused.get("price")) or to_gp(fused.get("price_text")) or 0.0

    new_gp = base_gp
    new_rarity = base_rarity
    chosen: list[dict] = []

    # --- FUNDAMENTAL (potency first, then optional property pairing) ---
    potency = 0
    potency_rune = None
    if rng.random() < fund_rate:
        fund_cands = _fundamental_candidates(all_runes, weapon_level, player_level)
        potency_cands = [
            r
            for r in fund_cands
            if parse_potency_rank(r.get("name")) >= 1 and not _is_weapon_fundamental_property(r)
        ]
        potency_rune = _weighted_pick_fundamental(potency_cands, rng, rcfg)
        if potency_rune:
            chosen.append(potency_rune)
            new_gp += (to_gp(potency_rune.get("price_text")) or 0.0)
            new_rarity = bump_rarity(new_rarity, (potency_rune.get("rarity") or "Common"))

            potency = parse_potency_rank(potency_rune.get("name"))
            fund_label = str(potency_rune.get("name", "")).strip()
            fused["_rune_fund_label"] = fund_label

            if potency > 0 and rng.random() < pair_rate:
                prop_cands = _weapon_fundamental_property_candidates(
                    all_runes,
                    potency_rank=potency,
                    party_level=player_level,
                )
                prop_rune = _pick_best_fundamental_property(prop_cands, potency, rng)
                if prop_rune:
                    chosen.append(prop_rune)
                    new_gp += (to_gp(prop_rune.get("price_text")) or 0.0)
                    new_rarity = bump_rarity(new_rarity, (prop_rune.get("rarity") or "Common"))
                    fused["_rune_fund_label"] = _format_fundamental_pair_label(
                        potency_rune, prop_rune
                    )

    # --- PROPERTIES (probabilistic gates per rules) ---
    prop_labels: List[str] = []
    if potency_rune:
        if potency > 0 and rng.random() < prop_rate:
            prop_cands = _property_candidates(all_runes, player_level, fused)
            picked_names = set()
            slots_taken = 0
            for _ in range(potency):
                if rng.random() >= per_slot:
                    continue
                pool = [r for r in prop_cands if r.get("name") not in picked_names]
                if not pool:
                    break
                r = _weighted_pick_by_rarity(pool, rng, rarity_weights)
                if r is None:
                    break
                picked_names.add(r.get("name"))
                chosen.append(r)
                new_gp += (to_gp(r.get("price_text")) or 0.0)
                new_rarity = bump_rarity(new_rarity, (r.get("rarity") or "Common"))
                prop_labels.append(str(r.get("name", "")).strip())
                slots_taken += 1
                if slots_taken >= potency:
                    break

            # Store property labels ONCE, after the loop finishes
            if prop_labels:
                fused["_rune_prop_labels"] = prop_labels

    # LEVEL = max(base, any chosen rune levels)
    try:
        base_lvl = int(fused.get("level") or 0)
    except Exception:
        base_lvl = 0
    try:
        rune_levels = [int(r.get("level") or 0) for r in (chosen or [])]
    except Exception:
        rune_levels = []
    if rune_levels:
        fused["level"] = max([base_lvl, *rune_levels])
    else:
        fused["level"] = base_lvl

    if chosen:  # at least one rune (fundamental or property)
        fused["category"] = "Runed Weapon"
        fused["is_magic_countable"] = True  # used by the summary counts
    else:
        fused["is_magic_countable"] = False

    # Save back
    fused["runes"]  = chosen
    fused["rarity"] = new_rarity
    fused["price"]  = _format_price(new_gp)
    return fused


def apply_armor_runes(
    armor: dict,
    *,
    player_level: int,
    runes_df: pd.DataFrame,
    rng: random.Random,
    rune_cfg: dict | None = None
) -> dict:
    """
    Armor version of rune application. Mirrors weapons:
      - choose fundamental (potency/resilient) with weights / caps
      - optionally choose property runes
      - bump price/rarity
      - bump level to max(base, runes)
      - set category = 'Runed Armor' and is_magic_countable=True when any rune lands
      - stash labels for final display (if you later want to compose armor names)
    """
    fused = dict(armor)
    fused.setdefault("_base_name", (armor.get("name") or "").strip())

    # NEW: do not apply armor runes to shields
    if _is_shield(fused):
        fused["runes"] = []
        fused["is_magic_countable"] = False
        return fused

    R = canonicalize_frame(pd.DataFrame(runes_df))
    if R.empty:
        fused["runes"] = []
        fused["is_magic_countable"] = False
        return fused

    # normalize
    for c in ("Type", "name", "rarity", "price_text", "source_table", "level"):
        if c in R.columns:
            if c == "level":
                R[c] = pd.to_numeric(R[c], errors="coerce").fillna(0).astype(int)
            else:
                R[c] = R[c].astype(str).str.strip()
    if "source_table" in R.columns:
        R = R[R["source_table"].str.lower().eq("runes")]

    all_runes = R.to_dict(orient="records")
    if not all_runes:
        fused["runes"] = []
        fused["is_magic_countable"] = False
        return fused

    rcfg = (rune_cfg or {}) if rune_cfg is not None else (CONFIG.get("runes", {}) or {})
    fund_cfg = (rcfg.get("fundamental") or {}) if isinstance(rcfg, dict) else {}
    fund_rate = float(fund_cfg.get("apply_rate", 1.0))
    pair_rate = _resolve_fundamental_property_rate(rcfg)
    prop_cfg = (rcfg.get("property") or {}) if isinstance(rcfg, dict) else {}
    prop_rate = float(prop_cfg.get("apply_rate", 0.30))
    per_slot  = float(prop_cfg.get("per_slot_rate", 0.30))
    rarity_weights = _resolve_rarity_weights(
        primary=(rcfg.get("rarity_weights") if isinstance(rcfg, dict) else None),
        fallback=CONFIG.get("rarity_weights", {"Common": 1.0}),
    )

    armor_level = int(fused.get("level") or 0)
    base_rarity = (fused.get("rarity") or "Common").strip().title()
    base_gp     = to_gp(fused.get("price")) or to_gp(fused.get("price_text")) or 0.0

    new_gp = base_gp
    new_rarity = base_rarity
    chosen: list[dict] = []

    # FUNDAMENTAL
    potency = 0
    potency_rune = None
    if rng.random() < fund_rate:
        fund_cands  = _fundamental_candidates_armor(all_runes, armor_level, player_level)
        potency_cands = [r for r in fund_cands if parse_potency_rank(r.get("name")) >= 1]
        potency_rune = _weighted_pick_fundamental(potency_cands, rng, rcfg)
        if potency_rune:
            chosen.append(potency_rune)
            new_gp     += (to_gp(potency_rune.get("price_text")) or 0.0)
            new_rarity  = bump_rarity(new_rarity, (potency_rune.get("rarity") or "Common"))

            potency = parse_potency_rank(potency_rune.get("name"))  # 1..3
            fused["_rune_fund_label"] = str(potency_rune.get("name", "")).strip()

            if potency > 0 and rng.random() < pair_rate:
                prop_cands = _armor_fundamental_property_candidates(
                    all_runes,
                    potency_rank=potency,
                    party_level=player_level,
                )
                prop_rune = _pick_best_fundamental_property(prop_cands, potency, rng)
                if prop_rune:
                    chosen.append(prop_rune)
                    new_gp     += (to_gp(prop_rune.get("price_text")) or 0.0)
                    new_rarity  = bump_rarity(new_rarity, (prop_rune.get("rarity") or "Common"))
                    fused["_rune_fund_label"] = _format_fundamental_pair_label(
                        potency_rune, prop_rune
                    )

    prop_labels: List[str] = []
    if potency_rune:
        # PROPERTIES
        if potency > 0 and rng.random() < prop_rate:
            prop_cands = _property_candidates_armor(all_runes, player_level, fused)
            picked_names = set()
            slots_taken = 0
            for _ in range(potency):
                if rng.random() >= per_slot:
                    continue
                pool = [r for r in prop_cands if r.get("name") not in picked_names]
                if not pool:
                    break
                r = _weighted_pick_by_rarity(pool, rng, rarity_weights)
                if r is None:
                    break
                picked_names.add(r.get("name"))
                chosen.append(r)
                new_gp     += (to_gp(r.get("price_text")) or 0.0)
                new_rarity  = bump_rarity(new_rarity, (r.get("rarity") or "Common"))
                prop_labels.append(str(r.get("name", "")).strip())
                slots_taken += 1
                if slots_taken >= potency:
                    break
    if prop_labels:
        fused["_rune_prop_labels"] = prop_labels

    # LEVEL bump
    try:
        base_lvl = int(fused.get("level") or 0)
    except Exception:
        base_lvl = 0
    try:
        rune_levels = [int(r.get("level") or 0) for r in (chosen or [])]
    except Exception:
        rune_levels = []
    fused["level"] = max([base_lvl, *rune_levels]) if rune_levels else base_lvl

    # CATEGORY / flags
    if chosen:
        fused["category"] = "Runed Armor"
        fused["is_magic_countable"] = True
        prev = (str(fused.get("tags", "")) or "").strip()
        fused["tags"] = ", ".join(t for t in [prev, "Runed"] if t).strip(", ")
    else:
        fused["is_magic_countable"] = False

    # Save back
    fused["runes"]  = chosen
    fused["rarity"] = new_rarity
    fused["price"]  = _format_price(new_gp)
    return fused

def apply_shield_runes(
    armor_row: dict,
    *,
    player_level: int,
    runes_df: pd.DataFrame,
    rng: random.Random,
    rune_cfg: dict | None = None
) -> dict:
    """
    Shields only get a single property rune (no fundamentals).
    - Candidate level <= player_level + 1
    - Update price/rarity/level
    - Category = 'Runed Shield'
    - is_magic_countable = True
    """
    fused = dict(armor_row)
    fused.setdefault("_base_name", (armor_row.get("name") or "").strip())

    # Only handle shields here; non-shields pass through unchanged
    if not _is_shield(fused):
        return fused

    # Load/normalize runes table
    R = canonicalize_frame(pd.DataFrame(runes_df))
    if R.empty:
        fused["runes"] = []
        fused["is_magic_countable"] = False
        return fused

    for c in ("Type", "name", "rarity", "price_text", "source_table", "level"):
        if c in R.columns:
            if c == "level":
                R[c] = pd.to_numeric(R[c], errors="coerce").fillna(0).astype(int)
            else:
                R[c] = R[c].astype(str).str.strip()
    if "source_table" in R.columns:
        R = R[R["source_table"].str.lower().eq("runes")]

    all_runes = R.to_dict(orient="records")
    if not all_runes:
        fused["runes"] = []
        fused["is_magic_countable"] = False
        return fused

    # Config: prefer shield_runes > armor_runes > runes
    rcfg = rune_cfg or CONFIG.get("shield_runes") or CONFIG.get("armor_runes") or CONFIG.get("runes") or {}
    if not isinstance(rcfg, dict):
        rcfg = {}
    prop_rate = float((rcfg.get("property") or {}).get("apply_rate", 0.30))
    rarity_weights = _resolve_rarity_weights(
        primary=rcfg.get("rarity_weights"),
        fallback=CONFIG.get("rarity_weights", {"Common": 1.0}),
    )

    # Base state
    base_rarity = (fused.get("rarity") or "Common").strip().title()
    base_gp     = to_gp(fused.get("price")) or to_gp(fused.get("price_text")) or 0.0
    new_gp      = base_gp
    new_rarity  = base_rarity

    # Single property rune gate
    chosen: list[dict] = []
    if rng.random() < prop_rate:
        pool = _shield_property_candidates(all_runes, player_level, fused)
        if pool:
            r = _weighted_pick_by_rarity(pool, rng, rarity_weights)
            if r is None:
                r = pool[rng.randint(0, len(pool) - 1)]
            chosen.append(r)
            new_gp     += (to_gp(r.get("price_text")) or 0.0)
            new_rarity  = bump_rarity(new_rarity, (r.get("rarity") or "Common"))
            # label for composer
            fused["_rune_prop_labels"] = [str(r.get("name", "")).strip()]

            # level bump = max(base, rune level)
            try:
                base_lvl = int(fused.get("level") or 0)
            except Exception:
                base_lvl = 0
            rune_lvl = int(r.get("level") or 0)
            fused["level"] = max(base_lvl, rune_lvl)

            # category / flag / tags
            fused["category"] = "Runed Shield"
            fused["is_magic_countable"] = True
            prev = (str(fused.get("tags", "")) or "").strip()
            fused["tags"] = ", ".join(t for t in [prev, "Runed"] if t).strip(", ")
        else:
            fused["is_magic_countable"] = False
    else:
        fused["is_magic_countable"] = False

    # Finalize
    fused["runes"]  = chosen
    fused["rarity"] = new_rarity
    fused["price"]  = _format_price(new_gp)
    return fused


def _apply_disposition(gp: float, disposition: str) -> float:
    mults = CONFIG.get("disposition_multipliers", {"greedy": 1.15, "fair": 1.0, "generous": 0.9})
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
