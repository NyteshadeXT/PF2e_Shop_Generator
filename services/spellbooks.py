
# services/spellbooks.py
# Spellbook generator for the PF2e Item Generator (Remaster rules)
from __future__ import annotations

from typing import Dict, List, Optional
from markupsafe import escape
from .randomness import get_rng
from .db import CONFIG, load_spells


# Add this import (uses your existing helper in utils.py)
try:
    from .utils import aon_spell_url
except Exception:
    # Safe fallback if running standalone
    from urllib.parse import quote_plus
    def aon_spell_url(name: str) -> str:
        q = quote_plus((name or "").strip())
        return f"https://2e.aonprd.com/Search.aspx?query={q}&type=spell&display=all"

TRADITIONS = ("Arcane", "Occult", "Divine", "Primal")
SPELLBOOK_SHOP_TYPES = {"Adventuring", "Arcane", "Scribe", "Temple"}

def _pick_tradition(shop_type: str) -> str:
    st = (shop_type or "").replace("_", " ").strip().title()
    if st == "Arcane":
        return "Arcane"
    if st == "Temple":
        return "Divine"
    return get_rng().choice(TRADITIONS)

def _pick_book_level(party_level: int) -> int:
    party_level = max(1, min(20, int(party_level or 1)))
    sign = 1 if get_rng().randint(1, 2) == 1 else -1
    delta = get_rng().randint(1, 3)
    lvl = party_level + sign * delta
    return max(1, min(20, lvl))

def _counts_for_book_level(level: int) -> Dict[int, int]:
    """Return the legacy Access count table with its original 1d4 variance."""
    L = int(level)
    c = {r: 0 for r in range(1, 11)}
    if not 1 <= L <= 20:
        return c

    if L == 20:
        base = {1: 4, 2: 3, 3: 3, 4: 3, 5: 3, 6: 2, 7: 2, 8: 1, 9: 1, 10: 1}
    else:
        highest_rank = (L + 1) // 2
        base = {rank: 3 for rank in range(1, highest_rank + 1)}
        if L % 2:
            base[highest_rank] = 2
        if L == 19:
            base[10] = 1

    for rank in sorted(base):
        c[rank] = base[rank] + get_rng().randint(1, 4)
    return c


def _roll_rarity() -> str:
    r = get_rng().randint(1, 100)
    if r <= 80:
        return "Common"
    if r <= 99:
        return "Uncommon"
    return "Rare"

def _load_spell_pool(
    spell_rows,
    *,
    tradition: str,
    themes: Optional[List[str]] = None,
) -> tuple[dict[tuple[int, str], list[dict]], dict[int, list[dict]]]:
    """Index one tradition from the shared in-memory spell reference table."""
    if spell_rows is None or spell_rows.empty:
        return {}, {rank: [] for rank in range(1, 11)}
    tradition_token = str(tradition or "").strip()
    mask = spell_rows["traditions"].astype(str).str.contains(
        tradition_token, case=False, regex=False, na=False
    )
    rows = spell_rows.loc[mask].to_dict(orient="records")
    themes_norm = [str(theme).strip().upper() for theme in (themes or []) if str(theme).strip()]
    if themes_norm:
        rows = [
            row for row in rows
            if any(theme in str(row.get("traits") or "").upper() for theme in themes_norm)
        ]

    by_key: dict[tuple[int, str], list[dict]] = {}
    for row in rows:
        rank = int(row.get("rank") or 0)
        rarity = str(row.get("rarity") or "Common").strip().title()
        row["rarity"] = rarity
        by_key.setdefault((rank, rarity), []).append(row)

    by_rank: dict[int, list[dict]] = {}
    for rank in range(1, 11):
        combined: list[dict] = []
        for rarity in ("Common", "Uncommon", "Rare"):
            combined.extend(by_key.get((rank, rarity), []))
        by_rank[rank] = combined
    return by_key, by_rank


def _pick_spell_rows(
    counts: Dict[int, int],
    by_key: dict[tuple[int, str], list[dict]],
    by_rank: dict[int, list[dict]],
) -> Dict[int, List[dict]]:
    """Apply the existing rarity rolls and duplicate-retry behavior in memory."""
    chosen_by_rank: Dict[int, List[dict]] = {rank: [] for rank in range(1, 11)}
    chosen_names: set[str] = set()
    for rank in range(1, 11):
        need = int(counts.get(rank, 0) or 0)
        safety = 1000
        while need > 0 and safety > 0:
            rarity = _roll_rarity()
            pool = by_key.get((rank, rarity), []) or by_rank.get(rank, [])
            if not pool:
                break
            pick = get_rng().choice(pool)
            name = str(pick.get("name") or "").strip()
            if not name or name in chosen_names:
                safety -= 1
                continue
            chosen_names.add(name)
            chosen_by_rank[rank].append(pick)
            need -= 1
            safety -= 1
    return chosen_by_rank

def _rank_suffix(r: int) -> str:
    if r == 1: return "st"
    if r == 2: return "nd"
    if r == 3: return "rd"
    return "th"

def _render_contents_html(spells_by_rank: Dict[int, List[dict]]) -> str:
    parts = ['<details class="sb-acc"><summary>Contents</summary><div class="sb-contents">']
    for r in range(1, 11):
        entries = spells_by_rank.get(r, [])
        if not entries:
            continue
        row_bits = []
        for e in entries:
            n = e.get("name", "").strip()
            src = (e.get("source") or "").strip()
            safe_name = escape(n)
            if not src or "paizo" in src.lower():
                safe_url = escape(aon_spell_url(n))
                row_bits.append(
                    f'<a href="{safe_url}" target="_blank" rel="noopener">{safe_name}</a>'
                )
            else:
                row_bits.append(
                    f'{safe_name} <span class="badge adjusted">3rd-Party: {escape(src)}</span>'
                )
        parts.append(f'<div class="sb-rank"><strong>{r}{_rank_suffix(r)} Rank</strong>: {", ".join(row_bits)}</div>')
    parts.append("</div></details>")
    return "".join(parts)

def _make_spellbook_item(tradition: str, book_level: int, chosen_by_rank: Dict[int, List[str]], total_cost: float) -> Dict:
    price_text = f"{int(total_cost)} gp" if total_cost > 0 else ""
    item = {
        "name": f"Spellbook ({tradition}) — Level {book_level}",
        "level": book_level,
        "rarity": "Common",
        "price": price_text,
        "price_text": price_text,
        "quantity": 1,
        "category": f"Spellbook - {tradition}",
        "source_table": "spellbook",
        "tags": f"tradition:{tradition}",
        "details_html": _render_contents_html(chosen_by_rank),
    }
    # assign after the dict is created
    item["_dedupe_key"] = f"{item['name']}#{get_rng().getrandbits(24):06x}"
    return item

def select_spellbooks(
    df,
    shop_type: str,
    party_level: int,
    shop_size: str,
    disposition: str,
    *,
    max_books: int = 2,
    sqlite_path: Optional[str] = None
) -> Dict:
    st_norm = (shop_type or "").replace("_", " ").strip().title()
    if st_norm not in SPELLBOOK_SHOP_TYPES:
        return {"items": [], "base_count": 0, "critical_added": 0}

    try:
        spell_rows = load_spells(sqlite_path=sqlite_path or CONFIG.get("sqlite_db_path"))
    except Exception:
        return {"items": [], "base_count": 0, "critical_added": 0}

    items: List[Dict] = []

    cfg = CONFIG.get("spellbooks", {}) or {}
    drop_rate = float(cfg.get("drop_rate", 0.35))
    max_books = int(cfg.get("max_books", max_books))

    size_norm = (shop_size or "").strip().lower()
    suggested = 1
    if size_norm in ("medium", "med"):
        suggested = 2
    elif size_norm in ("large", "grand", "huge"):
        suggested = 3
    target = min(max_books, suggested)

    def _total_spells(chosen: Dict[int, List[dict]]) -> int:
        return sum(len(v) for v in chosen.values())

    pool_cache: dict[str, tuple[dict, dict]] = {}
    for _ in range(target):
        # roll PER BOOK; on miss, skip creating a book entirely
        if get_rng().random() >= drop_rate:
            continue

        tradition = _pick_tradition(st_norm)
        book_level = _pick_book_level(party_level)

        # ✅ counts MUST be defined here (per book)
        counts = _counts_for_book_level(book_level)
        if tradition not in pool_cache:
            pool_cache[tradition] = _load_spell_pool(spell_rows, tradition=tradition)
        chosen_rows = _pick_spell_rows(counts, *pool_cache[tradition])

        # fill ALL ranks per count table (no per-rank drop gate)
        total_cost = sum(
            float(row.get("cost", 0) or 0)
            for rows in chosen_rows.values()
            for row in rows
        )

        # skip empty spellbooks
        if _total_spells(chosen_rows) == 0:
            continue

        display_rows = {
            rank: [
                {
                    "name": str(row.get("name") or "").strip(),
                    # Source is a book citation, not a publisher classification.
                    "source": "",
                }
                for row in rows
            ]
            for rank, rows in chosen_rows.items()
        }
        items.append(_make_spellbook_item(tradition, book_level, display_rows, total_cost))

    return {"items": items, "base_count": len(items), "critical_added": 0}


# --- Standalone Spellbook builder (for the new page) -------------------------

def build_spellbook(
    *,
    tradition: str,
    book_level: int,
    themes: Optional[List[str]] = None,
    sqlite_path: Optional[str] = None,
) -> List[Dict]:
    """
    Return a flat list of spells for a single spellbook, honoring the same per-rank counts
    used by select_spellbooks(), but for an explicit tradition + book level.
    Each spell = { name, level, traditions, traits, aon_target }
    """
    tradition = (tradition or "").strip().title()
    if tradition not in TRADITIONS:
        return []

    try:
        L = max(1, min(20, int(book_level or 1)))
    except Exception:
        L = 1

    # normalize themes
    themes_norm = []
    if themes:
        themes_norm = [t.strip().upper() for t in themes if str(t).strip()]

    try:
        spell_rows = load_spells(sqlite_path=sqlite_path or CONFIG.get("sqlite_db_path"))
    except Exception:
        return []

    counts = _counts_for_book_level(L)
    by_key, by_rank = _load_spell_pool(spell_rows, tradition=tradition, themes=themes_norm)
    chosen_rows = _pick_spell_rows(counts, by_key, by_rank)

    picked: List[Dict] = []
    for rows in chosen_rows.values():
        for row in rows:
            name = str(row.get("name") or "").strip()
            picked.append({
                "name": name,
                "level": int(row.get("rank") or 0),
                "traditions": row.get("traditions") or "",
                "traits": row.get("traits") or "",
                "rarity": row.get("rarity") or "Common",
                "source": row.get("source") or "",
                "aon_target": name,
            })

    picked.sort(key=lambda x: (x.get("level", 0), x.get("name", "")))
    return picked

