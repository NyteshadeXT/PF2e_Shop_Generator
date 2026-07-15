# services/db.py
import sqlite3, pandas as pd, logging
from contextlib import closing
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Tuple
from urllib.parse import quote_plus


from services.settings import CONFIG
from services.catalog_order import canonicalize_frame
logger = logging.getLogger(__name__)

def _path_signature(raw_path: str | None) -> Tuple[str | None, float | None]:
    """Return a stable signature for a filesystem path (resolved path + mtime)."""
    if not raw_path:
        return None, None
    try:
        path = Path(raw_path).resolve(strict=False)
        stat = path.stat()
        return str(path), float(stat.st_mtime)
    except FileNotFoundError:
        return str(Path(raw_path).resolve(strict=False)), None
    except OSError:
        return str(Path(raw_path).resolve(strict=False)), None


def _signature_from_parts(parts: Iterable[Tuple[str, Any]]) -> Tuple[Tuple[str, Any], ...]:
    return tuple(parts)


def _items_signature() -> Tuple[Tuple[str, Any], ...]:
    return _signature_from_parts([
        ("view", CONFIG.get("sqlite_view")),
        ("sqlite_db_path", _path_signature(CONFIG.get("sqlite_db_path"))),
    ])


_ITEMS_CACHE: pd.DataFrame | None = None
_ITEMS_SIGNATURE: Tuple[Tuple[str, Any], ...] | None = None
_ITEMS_LOCK = RLock()


def _adjustments_signature() -> Tuple[Tuple[str, Any], ...]:
    return _signature_from_parts([
        ("table", CONFIG.get("sqlite_adjustments_table")),
        ("sqlite_db_path", _path_signature(CONFIG.get("sqlite_db_path"))),
    ])


_ADJUSTMENTS_CACHE: pd.DataFrame | None = None
_ADJUSTMENTS_SIGNATURE: Tuple[Tuple[str, Any], ...] | None = None
_ADJUSTMENTS_LOCK = RLock()


def _materials_signature(material_key: Tuple[str, ...]) -> Tuple[Tuple[str, Any], ...]:
    return _signature_from_parts([
        ("material_types", material_key),
        ("sqlite_db_path", _path_signature(CONFIG.get("sqlite_db_path"))),
    ])


_MATERIALS_CACHE: dict[Tuple[str, ...], tuple[pd.DataFrame, Tuple[Tuple[str, Any], ...]]] = {}
_MATERIALS_LOCK = RLock()

_REFERENCE_CACHE: dict[
    tuple[str, str], tuple[pd.DataFrame, Tuple[str | None, float | None]]
] = {}
_REFERENCE_LOCK = RLock()


def _load_reference_query(
    name: str,
    query: str,
    *,
    sqlite_path: str | None = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Load stable reference data once per database version and process."""
    path = str(Path(sqlite_path or CONFIG["sqlite_db_path"]).resolve())
    key = (name, path)
    signature = _path_signature(path)
    with _REFERENCE_LOCK:
        cached = _REFERENCE_CACHE.get(key)
        if not force_refresh and cached and cached[1] == signature:
            return cached[0].copy(deep=False)
        with closing(sqlite3.connect(path)) as conn:
            frame = canonicalize_frame(pd.read_sql_query(query, conn))
        frame.attrs["reference_signature"] = signature
        _REFERENCE_CACHE[key] = (frame, signature)
        logger.info("Loaded %d %s reference rows", len(frame), name)
        return frame.copy(deep=False)


def load_spells(
    *, sqlite_path: str | None = None, force_refresh: bool = False
) -> pd.DataFrame:
    return _load_reference_query(
        "spells",
        """
        SELECT
            Name AS name,
            CAST(Rank AS INTEGER) AS rank,
            Tradition AS traditions,
            COALESCE(Rarity, 'Common') AS rarity,
            COALESCE(Cost, 0) AS cost,
            COALESCE(Traits, '') AS traits,
            COALESCE(Source, '') AS source
        FROM Spells
        ORDER BY rowid
        """,
        sqlite_path=sqlite_path,
        force_refresh=force_refresh,
    )


def load_formula_rows(*, force_refresh: bool = False) -> pd.DataFrame:
    return _load_reference_query(
        "formula prices",
        'SELECT * FROM "Formula"',
        force_refresh=force_refresh,
    )


def clear_reference_caches() -> None:
    """Clear process-local reference caches, primarily for tests and maintenance."""
    with _REFERENCE_LOCK:
        _REFERENCE_CACHE.clear()

def _load_sqlite():
    con = sqlite3.connect(CONFIG["sqlite_db_path"])
    try:
        df = canonicalize_frame(
            pd.read_sql_query(f"SELECT * FROM {CONFIG['sqlite_view']};", con)
        )
        rows = len(df)
        logger.info(
            "Loaded %d catalog rows from SQLite view %s", rows, CONFIG["sqlite_view"]
        )
        if rows == 0:
            raise RuntimeError(
                f"SQLite catalog view {CONFIG['sqlite_view']} returned no rows"
            )
        return df
    finally:
        con.close()

def load_items(force_refresh: bool = False) -> pd.DataFrame:
    """Load the main item catalog with simple caching."""
    global _ITEMS_CACHE, _ITEMS_SIGNATURE

    signature = _items_signature()
    with _ITEMS_LOCK:
        if not force_refresh and _ITEMS_CACHE is not None and _ITEMS_SIGNATURE == signature:
            return _ITEMS_CACHE.copy(deep=False)
    df = _load_sqlite()

    with _ITEMS_LOCK:
        _ITEMS_CACHE = df
        _ITEMS_SIGNATURE = _items_signature()
        return _ITEMS_CACHE.copy(deep=False)

# -------- Adjustments loader --------

def _empty_adjustments_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["name", "subtype", "rarity", "level", "price_text", "tags", "Source"]
    )

def _load_adjustments_sqlite() -> pd.DataFrame:
    table = CONFIG.get("sqlite_adjustments_table", "Adjustments")
    con = sqlite3.connect(CONFIG["sqlite_db_path"])
    try:
        df = pd.read_sql_query(f'SELECT * FROM "{table}";', con)
        rows = len(df)
        logger.info("Loaded %d adjustment rows from SQLite table %s", rows, table)
        return df
    finally:
        con.close()

def load_adjustments() -> pd.DataFrame:
    """
    Load the Adjustments table/view.
    Expected columns after normalization:
      name, subtype, rarity, level, price_text, tags, Source
    """
    global _ADJUSTMENTS_CACHE, _ADJUSTMENTS_SIGNATURE

    signature = _adjustments_signature()
    with _ADJUSTMENTS_LOCK:
        if _ADJUSTMENTS_CACHE is not None and _ADJUSTMENTS_SIGNATURE == signature:
            return _ADJUSTMENTS_CACHE.copy(deep=False)

    try:
        df = _load_adjustments_sqlite()
    except Exception:
        logger.warning("Adjustment data could not be loaded", exc_info=True)
        df = _empty_adjustments_df()

    if df.empty:
        return df

    # --- Normalize columns to what the logic expects ---
    rename = {
        "Name": "name",
        "Subtype": "subtype",
        "Rarity": "rarity",
        "ItemLevel": "level",
        "Level": "level",
        "Cost": "price_text",
        "PriceText": "price_text",
        "Price": "price_text",   # allow either Price or PriceText
        "Traits": "tags",
        "Tags": "tags",
        "Source": "Source",
    }
    have = set(df.columns)
    df = df.rename(columns={k: v for k, v in rename.items() if k in have})

    # Ensure required columns exist
    for col in ("name", "subtype", "rarity", "level", "price_text", "tags", "Source"):
        if col not in df.columns:
            df[col] = None

    # Types / trimming
    df["level"] = pd.to_numeric(df["level"], errors="coerce").fillna(0).astype(int)
    for c in ("name", "subtype", "rarity", "price_text", "tags", "Source"):
        df[c] = df[c].astype(str).str.strip()
    df = canonicalize_frame(df)

    with _ADJUSTMENTS_LOCK:
        _ADJUSTMENTS_CACHE = df
        _ADJUSTMENTS_SIGNATURE = _adjustments_signature()
        return _ADJUSTMENTS_CACHE.copy(deep=False)

def get_spells_by_rank(conn: sqlite3.Connection, rank: int):
    """
    Return list of dicts: {name, rarity, aon_link} for spells of a given rank.
    """
    q = """
        SELECT Name, Rarity
        FROM Spells
        WHERE Rank = ?
        ORDER BY Name COLLATE NOCASE, Rarity COLLATE NOCASE
    """
    cur = conn.execute(q, (rank,))
    rows = cur.fetchall()
    results = []
    for name, rarity in rows:
        # AON: use search so we don't need IDs
        aon_link = f"https://2e.aonprd.com/Search.aspx?q={quote_plus(name)}"
        results.append({
            "name": name,
            "rarity": (rarity or "Common").strip().title(),
            "aon_link": aon_link
        })
    return results

# -------- Materials loader --------
def load_materials(material_types: list[str]) -> pd.DataFrame:
    """
    Load material data from SQLite tables.
    material_types: A list like ['armor', 'weapon', 'shield']
    """
    global _MATERIALS_CACHE

    key = tuple(sorted({str(m).strip().lower() for m in (material_types or []) if str(m).strip()}))
    signature = _materials_signature(key)

    with _MATERIALS_LOCK:
        cached = _MATERIALS_CACHE.get(key)
        if cached and cached[1] == signature:
            return cached[0].copy(deep=False)

    if not key:
        return pd.DataFrame()

    all_dfs = []
    con = None
    try:
        con = sqlite3.connect(CONFIG["sqlite_db_path"])
        for mtype in key:
            table_name = f"{mtype}_material"
            try:
                df = pd.read_sql_query(f'SELECT * FROM "{table_name}";', con)
                logger.info("Loaded %d material rows from table %s", len(df), table_name)
                all_dfs.append(df)
            except Exception:
                logger.warning("Material table %s could not be loaded", table_name, exc_info=True)
    except Exception:
        logger.exception("Could not connect to SQLite for material loading")
        return pd.DataFrame()
    finally:
        if con:
            con.close()

    if not all_dfs:
        return pd.DataFrame()

    df = pd.concat(all_dfs, ignore_index=True)

    # --- Normalize columns to what the logic expects ---
    rename = {
        "Name": "name",
        "Rarity": "rarity",
        "ItemLevel": "level",
        "AddedPrice": "price_add",
        "AddedPricePerBulk": "price_add_per_bulk",
        "Prerequisite": "prerequisite",
    }
    have = set(df.columns)
    df = df.rename(columns={k: v for k, v in rename.items() if k in have})

    # Ensure required columns exist
    for col in ("name", "rarity", "level", "price_add", "price_add_per_bulk", "prerequisite"):
        if col not in df.columns:
            df[col] = None

    # Types / trimming
    df["level"] = pd.to_numeric(df["level"], errors="coerce").fillna(0).astype(int)
    df["price_add"] = pd.to_numeric(df["price_add"], errors="coerce").fillna(0)
    df["price_add_per_bulk"] = pd.to_numeric(df["price_add_per_bulk"], errors="coerce").fillna(0)
    for c in ("name", "rarity", "prerequisite"):
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip()
    df = canonicalize_frame(df)
        
    with _MATERIALS_LOCK:
        _MATERIALS_CACHE[key] = (df, _materials_signature(key))
        return df.copy(deep=False)    
    
def fetch_runes(conn, *, max_level: int = None):
    """
    Return all rune rows from the Runes source table, optionally filtered by level.
    Expecting columns: name, level, rarity, price, Type (e.g., 'Weapon Fundamental Rune' or 'Weapon Property Rune').
    """
    cur = conn.cursor()
    if max_level is None:
        cur.execute("""
            SELECT name, level, rarity, price, Type, source_table
            FROM items
            WHERE source_table = 'Runes'
        """)
    else:
        cur.execute("""
            SELECT name, level, rarity, price, Type, source_table
            FROM items
            WHERE source_table = 'Runes' AND level <= ?
        """, (max_level,))
    rows = cur.fetchall()
    # Normalize into dicts (match how you already do elsewhere)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in rows]
