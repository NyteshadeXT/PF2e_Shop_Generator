
# services/spellbooks.py
# Spellbook generator for the PF2e Item Generator (Remaster rules)
from __future__ import annotations

import sqlite3
from typing import Dict, List, Tuple, Optional
from .randomness import get_rng


# Add this import (uses your existing helper in utils.py)
try:
    from .utils import aon_spell_url
except Exception:
    # Safe fallback if running standalone
    from urllib.parse import quote_plus
    def aon_spell_url(name: str) -> str:
        q = quote_plus((name or "").strip())
        return f"https://2e.aonprd.com/Search.aspx?query={q}&type=spell&display=all"

try:
    # Local package import path in user's project
    from .db import CONFIG
except Exception:
    # Fallback for standalone execution during development
    CONFIG = {"sqlite_db_path": "data/items.sqlite"}

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
    """Direct translation of the Access table with 1d4 variance."""
    L = int(level)
    c = {r: 0 for r in range(1, 11)}
    d4 = lambda: get_rng().randint(1, 4)

    def setr(pairs: Dict[int, int]) -> None:
        c.update(pairs)

    if L == 1:
        setr({1: 2 + d4()})
    elif L == 2:
        setr({1: 3 + d4()})
    elif L == 3:
        setr({1: 3 + d4(), 2: 2 + d4()})
    elif L == 4:
        setr({1: 3 + d4(), 2: 3 + d4()})
    elif L == 5:
        setr({1: 3 + d4(), 2: 3 + d4(), 3: 2 + d4()})
    elif L == 6:
        setr({1: 3 + d4(), 2: 3 + d4(), 3: 3 + d4()})
    elif L == 7:
        setr({1: 3 + d4(), 2: 3 + d4(), 3: 3 + d4(), 4: 2 + d4()})
    elif L == 8:
        setr({1: 3 + d4(), 2: 3 + d4(), 3: 3 + d4(), 4: 3 + d4()})
    elif L == 9:
        setr({1: 3 + d4(), 2: 3 + d4(), 3: 3 + d4(), 4: 3 + d4(), 5: 2 + d4()})
    elif L == 10:
        setr({1: 3 + d4(), 2: 3 + d4(), 3: 3 + d4(), 4: 3 + d4(), 5: 3 + d4()})
    elif L == 11:
        setr({1: 3 + d4(), 2: 3 + d4(), 3: 3 + d4(), 4: 3 + d4(), 5: 3 + d4(), 6: 2 + d4()})
    elif L == 12:
        setr({1: 3 + d4(), 2: 3 + d4(), 3: 3 + d4(), 4: 3 + d4(), 5: 3 + d4(), 6: 3 + d4()})
    elif L == 13:
        setr({1: 3 + d4(), 2: 3 + d4(), 3: 3 + d4(), 4: 3 + d4(), 5: 3 + d4(), 6: 3 + d4(), 7: 2 + d4()})
    elif L == 14:
        setr({1: 3 + d4(), 2: 3 + d4(), 3: 3 + d4(), 4: 3 + d4(), 5: 3 + d4(), 6: 3 + d4(), 7: 3 + d4()})
    elif L == 15:
        setr({1: 3 + d4(), 2: 3 + d4(), 3: 3 + d4(), 4: 3 + d4(), 5: 3 + d4(), 6: 3 + d4(), 7: 3 + d4(), 8: 2 + d4()})
    elif L == 16:
        setr({1: 3 + d4(), 2: 3 + d4(), 3: 3 + d4(), 4: 3 + d4(), 5: 3 + d4(), 6: 3 + d4(), 7: 3 + d4(), 8: 3 + d4()})
    elif L == 17:
        setr({1: 3 + d4(), 2: 3 + d4(), 3: 3 + d4(), 4: 3 + d4(), 5: 3 + d4(), 6: 3 + d4(), 7: 3 + d4(), 8: 3 + d4(), 9: 2 + d4()})
    elif L == 18:
        setr({1: 3 + d4(), 2: 3 + d4(), 3: 3 + d4(), 4: 3 + d4(), 5: 3 + d4(), 6: 3 + d4(), 7: 3 + d4(), 8: 3 + d4(), 9: 3 + d4()})
    elif L == 19:
        setr({1: 3 + d4(), 2: 3 + d4(), 3: 3 + d4(), 4: 3 + d4(), 5: 3 + d4(), 6: 3 + d4(), 7: 3 + d4(), 8: 3 + d4(), 9: 3 + d4(), 10: 1 + d4()})
    elif L == 20:
        setr({1: 4 + d4(), 2: 3 + d4(), 3: 3 + d4(), 4: 3 + d4(), 5: 3 + d4(), 6: 2 + d4(), 7: 2 + d4(), 8: 1 + d4(), 9: 1 + d4(), 10: 1 + d4()})
    return c


def _roll_rarity() -> str:
    r = get_rng().randint(1, 100)
    if r <= 80:
        return "Common"
    if r <= 99:
        return "Uncommon"
    return "Rare"

def _fetch_spells(conn: sqlite3.Connection, *, rank: int, tradition: str, rarity: str) -> List[Dict]:
    cur = conn.cursor()
    q = """
        SELECT Name, Rank, Tradition, Rarity, COALESCE(Cost, 0) as Cost
        FROM Spells
        WHERE Rank = ?
          AND (UPPER(Tradition) LIKE '%' || UPPER(?) || '%')
          AND Rarity = ?
    """
    cur.execute(q, (rank, tradition, rarity))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]

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
            if not src or "paizo" in src.lower():
                row_bits.append(f'<a href="{aon_spell_url(n)}" target="_blank" rel="noopener">{n}</a>')
            else:
                row_bits.append(f'{n} <span class="badge adjusted">3rd-Party: {src}</span>')
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

    path = sqlite_path or CONFIG.get("sqlite_db_path")
    conn = None
    try:
        conn = sqlite3.connect(path)
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

    for _ in range(target):
        # roll PER BOOK; on miss, skip creating a book entirely
        if get_rng().random() >= drop_rate:
            continue

        tradition = _pick_tradition(st_norm)
        book_level = _pick_book_level(party_level)

        # ✅ counts MUST be defined here (per book)
        counts = _counts_for_book_level(book_level)

        chosen_by_rank: Dict[int, List[dict]] = {r: [] for r in range(1, 11)}
        chosen_set: set[str] = set()
        total_cost = 0.0

        # fill ALL ranks per count table (no per-rank drop gate)
        for rank in range(1, 11):
            need = counts.get(rank, 0)
            safety = 1000
            while need > 0 and safety > 0:
                rarity = _roll_rarity()
                pool = _fetch_spells(conn, rank=rank, tradition=tradition, rarity=rarity)
                if not pool:
                    any_pool: List[Dict] = []
                    for rar in ("Common", "Uncommon", "Rare"):
                        any_pool.extend(_fetch_spells(conn, rank=rank, tradition=tradition, rarity=rar))
                    pool = any_pool
                if not pool:
                    break

                pick = get_rng().choice(pool)
                name = (pick.get("Name") or pick.get("name") or "").strip()
                if not name:
                    safety -= 1
                    continue

                # de-dupe across the whole book
                if name in chosen_set:
                    safety -= 1
                    continue
                chosen_set.add(name)

                source = (pick.get("Source") or "").strip()
                chosen_by_rank[rank].append({"name": name, "source": source})

                try:
                    total_cost += float(pick.get("Cost", 0) or 0)
                except Exception:
                    pass

                need -= 1
                safety -= 1

        # skip empty spellbooks
        if _total_spells(chosen_by_rank) == 0:
            continue

        items.append(_make_spellbook_item(tradition, book_level, chosen_by_rank, total_cost))

    if conn:
        conn.close()
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

    path = sqlite_path or CONFIG.get("sqlite_db_path")
    try:
        conn = sqlite3.connect(path)
    except Exception:
        return []

    counts = _counts_for_book_level(L)
    chosen_set: set[str] = set()
    picked: List[Dict] = []

    def _pool_for(rank: int, rarity: str) -> List[Dict]:
        cur = conn.cursor()
        # include Traits for optional theme filtering
        q = """
            SELECT
              Name        AS name,
              Rank        AS level,
              Tradition   AS traditions,
              Rarity      AS rarity,
              COALESCE(Traits, '') AS traits,
              COALESCE(Source, '') AS source 
            FROM Spells
            WHERE Rank = ?
              AND (UPPER(Tradition) LIKE '%' || UPPER(?) || '%')
              AND Rarity = ?
        """
        cur.execute(q, (rank, tradition, rarity))
        rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
        if themes_norm:
            rows = [
                r for r in rows
                if any(t in (r.get("traits") or "").upper() for t in themes_norm)
            ]
        return rows

    try:
        for rank in range(1, 11):
            need = int(counts.get(rank, 0) or 0)
            safety = 1000
            while need > 0 and safety > 0:
                rarity = _roll_rarity()
                pool = _pool_for(rank, rarity)

                # fallback: try any rarity if the filtered pool is empty
                if not pool:
                    any_pool: List[Dict] = []
                    for rar in ("Common", "Uncommon", "Rare"):
                        any_pool.extend(_pool_for(rank, rar))
                    pool = any_pool

                if not pool:
                    break

                pick = get_rng().choice(pool)
                name = (pick.get("name") or "").strip()
                if not name or name in chosen_set:
                    safety -= 1
                    continue

                chosen_set.add(name)
                # normalize keys for the fragment template
                picked.append({
                    "name": name,
                    "level": int(pick.get("level") or 0),
                    "traditions": pick.get("traditions") or "",
                    "traits": pick.get("traits") or "",
                    "rarity": pick.get("rarity") or "Common",
                    "aon_target": name,   # for aon URL helper
                })
                need -= 1
                safety -= 1
    finally:
        try:
            conn.close()
        except Exception:
            pass

    picked.sort(key=lambda x: (x.get("level", 0), x.get("name", "")))
    return picked

