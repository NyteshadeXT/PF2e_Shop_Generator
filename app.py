# app.py — cleaned and production-ready (Render + SSE + Player View)

from flask import (
    Flask, render_template, request, send_file, redirect, abort,
    Response, stream_with_context, current_app, jsonify, url_for
)

import io, csv, json, os, queue, time, uuid, sqlite3, re, random
from collections import OrderedDict

# Third-party
import pandas as pd

# Core project imports from the services package
from services.db import load_items
from pathlib import Path
from services.logic import (
    select_mundane_items, select_weapons_items, select_armor_items,
    select_specific_magic_armor, select_specific_magic_weapons,
    select_magic_items, select_materials, CONFIG as LOGIC_CONFIG,
    select_formulas, apply_weapon_runes, apply_armor_runes, apply_shield_runes,
    GROUPS as ST_GROUPS, _load_runes_df, _compose_weapon_name, _compose_armor_name
)
from services.utils import rarity_counts, aon_url
from services.spellbooks import select_spellbooks
from services.spellbooks import build_spellbook
from services.player_views import (
    LiveChannelNotFound,
    SnapshotNotFound,
    current_token as persistent_current_token,
    live_channel as persistent_live_channel,
    load_snapshot as load_persistent_snapshot,
    normalize_channel,
    save_snapshot as save_persistent_snapshot,
)
from services.randomness import generation_rng, normalize_seed
    
from copy import deepcopy

# Optional: debug blueprint (if exists)
try:
    from services.debug import bp as debug_bp
except Exception:
    debug_bp = None

app = Flask(__name__)
if debug_bp is not None:
    app.register_blueprint(debug_bp, url_prefix="/debug")

# Make AoN helper available in Jinja
app.jinja_env.globals["aon_url"] = aon_url

# use the already-imported LOGIC_CONFIG from services.logic
DB_PATH = LOGIC_CONFIG.get("sqlite_db_path", "data/pf2e.sqlite")

# ----------------------------
# Helper utilities
# ---------------------------- 
def _norm_str(x):
    if x is None:
        return ""
    try:
        return str(x).strip()
    except Exception:
        return ""


def _to_int(x):
    try:
        if x is None or x == "":
            return 0
        return int(float(x))
    except Exception:
        return 0


def _count_crit(items):
    return sum(1 for it in (items or []) if it.get("critical"))


def get_shop_types(df: pd.DataFrame):
    if "shop_type" in df.columns and df["shop_type"].dropna().size:
        return sorted(x for x in df["shop_type"].dropna().unique())
    return LOGIC_CONFIG.get("default_shop_types", [])


def _common_inputs():
    shop_type = (request.form.get("shop_type") or "General").strip()
    shop_size = (request.form.get("shop_size") or "medium").strip()
    disposition = (request.form.get("disposition") or "fair").strip()
    try:
        party_level = int(request.form.get("party_level") or 5)
    except Exception:
        party_level = 5
    return shop_type, shop_size, disposition, party_level

def _open_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _row_has_all_themes(row_traits: str, themes: list[str]) -> bool:
    """Case-insensitive AND filter: all theme terms must appear in the spell's traits"""
    if not themes:
        return True
    traits = [t.strip().lower() for t in (row_traits or "").split(",")]
    themes_norm = [x.strip().lower() for x in themes if x.strip()]
    return all(any(theme in tr for tr in traits) for theme in themes_norm)

def _build_payload(df, shop_type, shop_size, disposition, party_level):
    """Mirror results-building logic for a safe Player View fallback."""
    mnd = select_mundane_items(df, shop_type, party_level, shop_size, disposition)
    mat = select_materials(df, shop_type, party_level, shop_size, disposition)
    arm = select_armor_items(df, shop_type, party_level, shop_size, disposition)
    wep = select_weapons_items(df, shop_type, party_level, shop_size, disposition)
    mag = select_magic_items(df, shop_type, party_level, shop_size, disposition)
    frm = select_formulas(df, shop_type, party_level, shop_size, disposition)

    window = (mag.get("window") if isinstance(mag, dict) else None) or (party_level, party_level)

    return {
        "mundane_items":   mnd.get("items", []),
        "materials_items": mat.get("items", []),
        "armor_items":     arm.get("items", []),
        "weapons_items":   wep.get("items", []),
        "magic_items":     mag.get("items", []) if isinstance(mag, dict) else [],
        "formulas_items":  frm.get("items", []),
        "window": window,
    }

# --- Magic Item Builder: ONLY override fundamental apply_rate ---------------
from copy import deepcopy
from flask import request

_FUND_KEYS   = {"fundamental", "fundamentals", "baseline"}  # common container keys
_PROP_KEYS   = {"property", "properties", "property_runes", "weapon_properties", "armor_properties"}
_FUND_LABELS = {"fundamental"}  # for fields like group/category/type/rune_type
# Names that are clearly fundamentals in PF2e remaster (best-effort, safe to include)
_FUND_NAME_HINTS = {"weapon potency", "armor potency", "shield potency", "striking", "resilient", "reinforcing"}

def _looks_fundamental_node(node: dict) -> bool:
    """Heuristic: is this node describing fundamental runes?"""
    if not isinstance(node, dict):
        return False
    # explicit flags in the node
    for k in ("group", "category", "type", "rune_type", "slot", "class"):
        v = str(node.get(k) or "").strip().lower()
        if v in _FUND_LABELS:
            return True
    # name-based hint (e.g., "Armor Potency +1", "Striking", etc.)
    name = str(node.get("name") or "").strip().lower()
    if any(h in name for h in _FUND_NAME_HINTS):
        return True
    return False

def _force_fundamental_apply_rate_only(cfg, rate: float = 1.0):
    """
    Recursively walk a rune config and set apply_rate ONLY on 'fundamental' sections.
    Property rune rates are not modified.
    Supports several config shapes:
      - item_runes = { fundamental: {...}, properties: {...} }
      - item_runes = { groups: [{group:'fundamental', ...}, {group:'property', ...}] }
      - item_runes = { fundamental_apply_rate: X, property_apply_rate: Y, ... }
      - nested dict/list structures where a node advertises fundamental via fields
    """
    if isinstance(cfg, dict):
        # Case A: explicit top-level scalar controls
        if "fundamental_apply_rate" in cfg:
            cfg["fundamental_apply_rate"] = rate
        # NOTE: intentionally DO NOT touch property_apply_rate
        # if "property_apply_rate" in cfg: (leave it alone)

        # Case B: direct 'fundamental' container
        for k, v in list(cfg.items()):
            kl = str(k).strip().lower()
            if kl in _FUND_KEYS:
                _force_fundamental_apply_rate_only(v, rate)  # recurse into fundamental subtree
                # also set rate on the container itself if it has an apply_rate
                if isinstance(v, dict) and "apply_rate" in v:
                    v["apply_rate"] = rate
                continue

            # Skip known property containers entirely
            if kl in _PROP_KEYS:
                # do NOT recurse into property* containers
                continue

            # Generic recursion: if this node itself declares it's fundamental, set its rate
            if isinstance(v, dict):
                if _looks_fundamental_node(v) and "apply_rate" in v:
                    v["apply_rate"] = rate
                _force_fundamental_apply_rate_only(v, rate)
            elif isinstance(v, list):
                _force_fundamental_apply_rate_only(v, rate)

    elif isinstance(cfg, list):
        for x in cfg:
            if isinstance(x, dict):
                # Grouped configs: [{group:'fundamental', apply_rate: ...}, ...]
                if _looks_fundamental_node(x) and "apply_rate" in x:
                    x["apply_rate"] = rate
            _force_fundamental_apply_rate_only(x, rate)

def _runes_cfg_for_request(item_type: str) -> dict:
    """
    Build the runes config for this request:
      - Start from LOGIC_CONFIG[item_type+'_runes'] (or LOGIC_CONFIG['runes']).
      - If the caller is the Magic Item Builder, force ONLY fundamental apply_rate=100%.
      - Leave property rune rates untouched.
    """
    base = deepcopy(
        LOGIC_CONFIG.get(f"{item_type}_runes")
        or LOGIC_CONFIG.get("runes")
        or {}
    )
    if request.path.startswith("/api/magic-builder/"):
        _force_fundamental_apply_rate_only(base, 1.0)
    return base

# ----------------------------
# Real-time broadcaster (SSE)
# ----------------------------
_subscribers: dict[str, list[queue.Queue]] = {}   # channel -> queues
_latest_roll_id: dict[str, str] = {}              # channel -> last id

_SNAPSHOT_CACHE_MAX = 100
_snapshot_cache: OrderedDict[tuple[str, str], dict] = OrderedDict()


def _cache_snapshot(channel: str, roll_id: str, snapshot: dict | None) -> None:
    """Store a snapshot for later retrieval by late joiners."""
    if not channel or not roll_id or not snapshot:
        return

    key = (channel, roll_id)
    _snapshot_cache[key] = deepcopy(snapshot)
    _snapshot_cache.move_to_end(key)

    while len(_snapshot_cache) > _SNAPSHOT_CACHE_MAX:
        _snapshot_cache.popitem(last=False)


def _get_snapshot(channel: str, roll_id: str) -> dict | None:
    snap = _snapshot_cache.get((channel, roll_id))
    if snap is None:
        return None
    return deepcopy(snap)

def _subscribe(channel: str) -> queue.Queue:
    q = queue.Queue(maxsize=10)
    _subscribers.setdefault(channel, []).append(q)
    return q


def _publish(channel: str, roll_id: str) -> None:
    """Publish a new roll id to all subscribers of a channel."""
    _latest_roll_id[channel] = roll_id
    for q in _subscribers.get(channel, [])[:]:
        try:
            q.put_nowait(roll_id)
        except Exception:
            try:
                _subscribers[channel].remove(q)
            except ValueError:
                pass


def _current_roll_id(channel: str) -> str:
    try:
        return persistent_current_token(channel)
    except (OSError, sqlite3.Error, ValueError):
        current_app.logger.exception("Unable to read the current Player View token")
        return _latest_roll_id.get(channel, "")


@app.route("/events")
def sse_events():
    """Server-Sent Events endpoint for live updates on new rolls."""
    try:
        channel = normalize_channel(request.args.get("channel"))
    except ValueError as exc:
        abort(400, str(exc))
    q = _subscribe(channel)

    @stream_with_context
    def event_stream():
        # Send last known id immediately for late joiners
        last = _current_roll_id(channel)
        if last:
            yield f"event: init\\ndata: {last}\\n\\n"

        heartbeat_every = 25
        last_beat = time.time()
        try:
            while True:
                try:
                    timeout = max(1, heartbeat_every - int(time.time() - last_beat))
                    rid = q.get(timeout=timeout)
                    yield f"data: {rid}\\n\\n"
                except queue.Empty:
                    # heartbeat comment to keep proxies from buffering
                    yield ": keep-alive\\n\\n"
                    last_beat = time.time()
        finally:
            # remove this subscriber
            try:
                _subscribers[channel].remove(q)
            except ValueError:
                pass

    return Response(event_stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.route("/version")
def version():
    """Lightweight polling fallback: returns the current roll id for a channel."""
    try:
        channel = normalize_channel(request.args.get("channel"))
    except ValueError as exc:
        abort(400, str(exc))
    return {"roll_id": _current_roll_id(channel)}


@app.get("/live/<live_token>")
def live_view(live_token: str):
    """Stable player-facing URL that follows a campaign's newest snapshot."""
    try:
        state = persistent_live_channel(live_token)
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


@app.get("/api/live/<live_token>/version")
def live_version(live_token: str):
    """Persistent polling endpoint; safe across workers and service restarts."""
    try:
        state = persistent_live_channel(live_token)
    except (LiveChannelNotFound, ValueError):
        abort(404)
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Unable to poll live Player View")
        abort(503)
    response = jsonify(state)
    response.headers["Cache-Control"] = "no-store"
    return response


# Optional: preserve the existing /health link in index.html
@app.get("/health")
def health():
    return {"ok": True}



# ----------------------------
# Routes
# ----------------------------
@app.get("/favicon.ico")
def favicon():
    return ("", 204)
    
@app.route("/player-view", methods=["GET", "POST"])
def player_view():
    data = request.values
    raw = data.get("snapshot")
    try:
        channel = normalize_channel(data.get("channel"))
    except ValueError as exc:
        abort(400, str(exc))
    roll_id = (data.get("roll_id") or "").strip()
    live_token = (request.args.get("live") or "").strip().lower()
    if live_token:
        try:
            live_state = persistent_live_channel(live_token)
        except (LiveChannelNotFound, ValueError):
            return render_template("player_view_missing.html"), 404
        except (OSError, sqlite3.Error):
            abort(503, "Live Player View storage is temporarily unavailable.")
        if live_state["channel"] != channel:
            abort(404)

    if request.method == "GET" and not raw and not roll_id:
        return redirect("/")

    lists: dict = {}
    meta: dict = {}
    snapshot_payload: dict | None = None

    # 1) Prefer exact GM snapshot if present
    if raw:
        try:
            snapshot_payload = json.loads(raw) or {}
        except Exception as e:
            current_app.logger.exception("Invalid snapshot JSON posted to /player-view: %s", e)
            lists, meta = {}, {}
    elif request.method == "GET" and roll_id:
        try:
            snapshot_payload = load_persistent_snapshot(roll_id, channel)
        except SnapshotNotFound:
            return render_template(
                "player_view_missing.html", channel=channel, roll_id=roll_id
            ), 404
        except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
            current_app.logger.exception("Unable to load Player View snapshot")
            abort(503, "Player View storage is temporarily unavailable.")

    if isinstance(snapshot_payload, dict) and snapshot_payload:
        if "lists" in snapshot_payload or "shop" in snapshot_payload:
            lists = snapshot_payload.get("lists") or {}
            meta = snapshot_payload.get("shop") or {}
        else:
            # tolerate a flat snapshot (legacy format)
            lists = {
                "mundane_items":  snapshot_payload.get("mundane_items", []),
                "material_items": snapshot_payload.get("material_items", []) or snapshot_payload.get("materials_items", []),
                "armor_items":    snapshot_payload.get("armor_items", []),
                "weapon_items":   snapshot_payload.get("weapon_items", []),
                "magic_items":    snapshot_payload.get("magic_items", []),
                "formula_items":  snapshot_payload.get("formula_items", []),
            }
            meta = {
                "shop_type":   snapshot_payload.get("shop_type"),
                "shop_size":   snapshot_payload.get("shop_size"),
                "disposition": snapshot_payload.get("disposition"),
                "party_level": snapshot_payload.get("party_level"),
                "seed":        snapshot_payload.get("seed"),
                "window":      snapshot_payload.get("window"),
            }

    if not lists:
        abort(400, "Player View snapshot is missing or invalid.")

    # A POST carries the exact snapshot. Persist it before returning a shareable URL.
    if request.method == "POST" and snapshot_payload and roll_id:
        try:
            save_persistent_snapshot(
                roll_id, channel, snapshot_payload, advance_channel=False
            )
            _cache_snapshot(channel, roll_id, snapshot_payload)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            current_app.logger.exception("Unable to persist posted Player View snapshot")
            abort(503, "Player View storage is temporarily unavailable.")

    shop_name = _norm_str(meta.get("shop_name") or meta.get("name"))
    if not shop_name:
        shop_name = None

    default_label = (_norm_str(meta.get("shop_type")) or "Shop").title()
    page_title = f"Player View — {shop_name or default_label}"
    return render_template(
        "results_player.html",   # ensure this file exists in templates/
        page_title=page_title,
        shop_name=shop_name,
        shop_type=meta.get("shop_type"),
        seed=meta.get("seed"),
        mundane_items=lists.get("mundane_items", []),
        material_items=lists.get("material_items", []),
        armor_items=lists.get("armor_items", []),
        weapon_items=lists.get("weapon_items", []),
        magic_items=lists.get("magic_items", []),
        formula_items=lists.get("formula_items", []),
        aon_url=aon_url,
        channel=channel,
        roll_id=roll_id,
        live_token=live_token,
    )

@app.get("/")
def index():
    df = load_items()
    shop_types = get_shop_types(df)
    dispositions = list(LOGIC_CONFIG.get("disposition_multipliers", {}).keys()) or ["fair"]
    return render_template(
        "index.html",
        shop_types=shop_types,
        shop_type=None,
        shop_size="medium",
        disposition="fair",
        dispositions=dispositions,
        party_level=5,
        seed="",
    )

@app.route("/query", methods=["GET", "POST"])
def query():
    data = request.values  # supports both .args (GET) and .form (POST)
    df = load_items()

    # Inputs from the form
    shop_type   = (data.get("shop_type") or "").strip()
    shop_size   = (data.get("shop_size") or "medium").strip().lower()
    disposition = (data.get("disposition") or "fair").strip().lower()
    shop_name   = (data.get("shop_name") or "").strip()
    try:
        party_level = int(data.get("party_level") or 5)
    except Exception:
        party_level = 5
    try:
        generation_seed = normalize_seed(data.get("seed"))
    except ValueError as exc:
        abort(400, str(exc))

    # Run selections
    with generation_rng(generation_seed):
        mundane_result   = select_mundane_items(df, shop_type, party_level, shop_size, disposition)
        armor_basic      = select_armor_items(df, shop_type, party_level, shop_size, disposition)
        weapons_result   = select_weapons_items(df, shop_type, party_level, shop_size, disposition)
        armor_magic      = select_specific_magic_armor(df, shop_type, party_level, shop_size, disposition)
        weapon_magic     = select_specific_magic_weapons(df, shop_type, party_level, shop_size, disposition)
        magic_basic      = select_magic_items(df, shop_type, party_level, shop_size, disposition)
        material_result  = select_materials(df, shop_type, party_level, shop_size, disposition)
        result_formulas  = select_formulas(df, shop_type, party_level, shop_size, disposition)
        spellbook_result = select_spellbooks(
            df=df,
            shop_type=shop_type,
            party_level=party_level,
            shop_size=shop_size,
            disposition=disposition,
        )

    # Lists actually rendered in the UI
    material_items = (material_result.get("items") or [])
    mundane_items  = (mundane_result.get("items") or [])
    magic_armor    = (armor_magic.get("items") or [])
    magic_weapons  = (weapon_magic.get("items") or [])
    armor_items    = (armor_basic.get("items") or []) + magic_armor
    weapon_items   = (weapons_result.get("items") or []) + magic_weapons
    magic_items    = (magic_basic.get("items") or [])
    magic_items   += (spellbook_result.get("items") or [])

    # Helper: unique-by (name, price, rarity, level)
    def _uniq(items):
        seen, out = set(), []
        for it in items or []:
            key = (
                (it.get("name") or "").strip(),
                (it.get("price") or it.get("price_text") or "").strip(),
                (it.get("rarity") or "").strip(),
                int((it.get("level") or 0) or 0),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        return out

    # Partition for counts
    runed_weapons = [w for w in weapon_items if (w.get("category") == "Runed Weapon" or w.get("is_magic_countable"))]
    weapons_nonruned = [w for w in weapon_items if w not in runed_weapons]

    runed_armor = [a for a in armor_items if (a.get("category") == "Runed Armor" or a.get("is_magic_countable"))]
    armor_nonruned = [a for a in armor_items if a not in runed_armor]

    mundane_u   = _uniq(mundane_items)
    materials_u = _uniq(material_items)
    armor_u     = _uniq(armor_items)
    weapons_u   = _uniq(weapons_nonruned)
    magic_u     = _uniq(magic_items + magic_armor + magic_weapons + runed_weapons + runed_armor)

    # Rarity histogram over everything shown
    counts = rarity_counts(mundane_items + material_items + armor_items + weapon_items + magic_items)

    def _count_crit(items):
        return sum(1 for it in (items or []) if it.get("critical"))

    picked = {
        "mundane":   len(mundane_u),
        "materials": len(materials_u),
        "armor":     len(armor_u),
        "weapons":   len(weapons_u),   # runed excluded here
        "magic":     len(magic_u),     # runed included here
        "formulas":  len(result_formulas.get("items", [])),
        "critical": (
            _count_crit(mundane_items)
            + _count_crit(material_items)
            + _count_crit(armor_items)
            + _count_crit(weapons_nonruned)
            + _count_crit(magic_armor)
            + _count_crit(magic_weapons)
            + _count_crit(magic_items)
            + _count_crit(runed_weapons)
        ),
        "critical_mundane":      _count_crit(mundane_items),
        "critical_materials":    _count_crit(material_items),
        "critical_armor_shield": _count_crit(armor_items),
        "critical_weapons":      _count_crit(weapons_nonruned),
        "critical_magic":        (
            _count_crit(magic_armor)
            + _count_crit(magic_weapons)
            + _count_crit(magic_items)
            + _count_crit(runed_weapons)
        ),
    }

    # Snapshot for Player View
    magic_window = None
    try:
        if isinstance(magic_basic, dict):
            magic_window = magic_basic.get("window")
    except Exception:
        magic_window = None

    snapshot = {
        "shop": {
            "shop_name": shop_name,
            "shop_type": shop_type,
            "shop_size": shop_size,
            "disposition": disposition,
            "party_level": party_level,
            "seed": generation_seed,
            "window": magic_window,
        },
        "lists": {
            "mundane_items": mundane_items,
            "material_items": material_items,
            "armor_items": armor_items,
            "weapon_items": weapon_items,
            "magic_items": magic_items,
            "formula_items": result_formulas.get("items", []),
        },
    }

    try:
        channel = normalize_channel(data.get("channel"))
    except ValueError as exc:
        abort(400, str(exc))
    roll_id = uuid.uuid4().hex
    try:
        # The stored snapshot is authoritative. Notify live views only after commit.
        live_token = save_persistent_snapshot(roll_id, channel, snapshot)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        current_app.logger.exception("Unable to persist generated Player View snapshot")
        abort(503, "Player View storage is temporarily unavailable.")
    _cache_snapshot(channel, roll_id, snapshot)
    _publish(channel, roll_id)

    return render_template(
        "results.html",
        shop_type=shop_type,
        shop_size=shop_size,
        disposition=disposition,
        shop_name=shop_name,
        party_level=party_level,
        seed=generation_seed,
        picked=picked,
        counts=counts,
        mundane_items=mundane_items,
        material_items=material_items,
        armor_items=armor_items,
        weapon_items=weapon_items,
        magic_items=magic_items,
        formula_items=result_formulas.get("items", []),
        aon_url=aon_url,
        snapshot=snapshot,
        window=magic_window,
        roll_id=roll_id,
        channel=channel,
        live_token=live_token,
    )

# Standalone Spellbook page
@app.get("/spellbooks")
def spellbooks_page():
    return render_template(
        "spellbook_page.html",
        max_level=1,
        aon_url=aon_url,
    )

# JSON → fragment API for both the page and the mini-tool on index.html
@app.post("/api/spellbooks/generate")
def api_generate_spellbook():
    data = request.get_json(force=True) or {}

    tradition = (data.get("tradition") or "").strip().title()
    if tradition not in ("Arcane", "Divine", "Occult", "Primal"):
        return jsonify(ok=False, error="Invalid or missing tradition"), 400

    try:
        max_level = max(1, min(10, int(data.get("max_level") or 1)))
    except Exception:
        max_level = 1

    # themes can be a list or comma-separated string; normalize to list[str]
    raw_themes = data.get("themes") or []
    if isinstance(raw_themes, str):
        themes = [t.strip() for t in raw_themes.split(",") if t.strip()]
    else:
        themes = [str(t).strip() for t in raw_themes if str(t).strip()]

    # Use the library function to build a book directly
    spells = build_spellbook(tradition=tradition, book_level=max_level, themes=themes)

    return jsonify(ok=True, spells=spells)

@app.get("/spellbooks/view")
def spellbooks_view():
    tradition = (request.args.get("tradition") or "").strip().title()
    try:
        max_level = int(request.args.get("max_level") or 1)
    except Exception:
        max_level = 1

    themes_raw = request.args.get("themes") or ""
    themes = [t.strip() for t in themes_raw.split(",") if t.strip()]

    spells = build_spellbook(tradition=tradition, book_level=max_level, themes=themes)

    return render_template(
        "spellbook_page.html",
        spells=spells,
        tradition=tradition,
        max_level=max_level,
        themes=themes,
        aon_url=aon_url,
    )

# --- Magic Item Builder API ----------------------------------------------
def _norm(s):
    return (str(s or "").strip().lower())

def _st_norm(s: str) -> str:
    import re
    # collapse to snake-ish: "Specific Magic Weapons" -> "specific_magic_weapons"
    return re.sub(r'[^a-z0-9]+', '_', str(s or '').lower()).strip('_')

def _is_shield_row(row: dict) -> bool:
    sub = _norm(row.get("subtype") or row.get("Subtype"))
    cat = _norm(row.get("category"))
    nm  = _norm(row.get("name"))
    return ("shield" in sub) or ("shield" in cat) or ("shield" in nm)

# --- Magic Item Builder: robust base-name provider --------------------------
def _lc(s): return str(s or "").strip().lower()
def _tokset(val: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(val or "").lower()))

def _st_norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', str(s or '').lower()).strip('_')

def _filter_by_sources(d: pd.DataFrame, prefer: list[str], fallback_group: str|None) -> pd.DataFrame:
    """Filter by normalized source_table; if empty, optionally fallback to ST_GROUPS[fallback_group]."""
    d = d.copy()
    d["__st"] = d["source_table"].astype(str).map(_st_norm)
    want = {_st_norm(x) for x in prefer}
    out = d[d["__st"].isin(want)]
    if out.empty and fallback_group and fallback_group in ST_GROUPS:
        alts = {_st_norm(x) for x in ST_GROUPS.get(fallback_group, [])}
        out = d[d["__st"].isin(alts)]
    return out

@app.get("/api/magic-builder/bases")
def api_mib_bases():
    try:
        t          = _lc(request.args.get("type"))
        max_level  = int(request.args.get("max_level") or 1)
        subtype_in = _lc(request.args.get("subtype"))
        armor_in   = _lc(request.args.get("armor_type"))

        if t not in ("weapon","armor","shield"):
            return jsonify(ok=False, error="Invalid type"), 400

        df = load_items()
        if df is None or df.empty:
            current_app.logger.info("mib_bases: no data")
            return jsonify(ok=True, names=[])

        d = df.copy()
        # normalize columns we need
        for col in ("name","category","type","source_table","level"):
            if col not in d.columns:
                d[col] = ""
        d["name"]        = d["name"].astype(str).str.strip()
        d["category_lc"] = d["category"].astype(str).str.strip().str.lower()
        d["itype"]       = d["type"].astype(str).str.strip().str.lower()
        d["source_table"]= d["source_table"].astype(str).str.strip()
        d["level"]       = pd.to_numeric(d["level"], errors="coerce").fillna(0).astype(int)

        # level cap
        d = d[d["level"] <= max_level]

        # prefer exact source tables; fallback to your ST_GROUPS if empty
        if t == "weapon":
            pool = _filter_by_sources(d, ["weapon_basic"], "weapons")
            pool = pool[pool["category_lc"].str.contains("weapon", na=False)]
            if subtype_in:
                eq = pool["itype"].eq(subtype_in)
                if not eq.any():
                    toks = set(re.findall(r"[a-z0-9]+", subtype_in))
                    eq = pool["itype"].apply(lambda v: toks.issubset(set(re.findall(r"[a-z0-9]+", v))))
                pool = pool[eq]

        elif t == "armor":
            pool = _filter_by_sources(d, ["armor_basic"], "armor")
            pool = pool[pool["category_lc"].str.contains("armor", na=False) & ~pool["category_lc"].str.contains("shield", na=False)]
            if armor_in in ("light","medium","heavy"):
                expected = f"{armor_in} armor"
                eq = pool["itype"].eq(expected)
                if not eq.any():
                    eq = pool["itype"].str.contains(rf"\b{re.escape(armor_in)}\b", na=False)
                pool = pool[eq]

        else:  # shield
            # ✅ use shield_basic for shields
            pool = _filter_by_sources(d, ["shield_basic"], "shields" if "shields" in ST_GROUPS else "armor")
            # If we had to fall back (no shield_basic rows), tighten by 'shield' keywords
            if not pool["source_table"].str.contains("shield_basic", case=False, na=False).any():
                is_shield = (
                    pool["category_lc"].str.contains("shield", na=False)
                    | pool["itype"].str.contains("shield", na=False)
                    | pool["name"].str.lower().str.contains("shield", na=False)
                )
                pool = pool[is_shield]

        names = sorted(pool["name"].dropna().unique().tolist())[:200]
        if not names:
            # helpful logging to see what's in the pool
            current_app.logger.info("mib_bases: type=%s subtype=%s armor=%s max=%s -> 0 names; sample itype: %s; sample sources: %s",
                                    t, subtype_in, armor_in, max_level,
                                    pool["itype"].value_counts().head(10).to_dict(),
                                    pool["source_table"].value_counts().head(10).to_dict())
        else:
            current_app.logger.info("mib_bases: type=%s subtype=%s armor=%s max=%s -> %d names",
                                    t, subtype_in, armor_in, max_level, len(names))
        return jsonify(ok=True, names=names)

    except Exception as e:
        current_app.logger.exception("mib_bases error")
        return jsonify(ok=False, error=str(e)), 500

        
@app.post("/api/magic-builder/build")
def api_mib_build():
    data = request.get_json(force=True) or {}
    t = (data.get("item_type") or "").strip().lower()
    try:
        L = int(data.get("max_level") or 1)
    except Exception:
        L = 1
    base_name = (data.get("base_name") or "").strip()

    if t not in ("weapon","armor","shield"):
        return jsonify(ok=False, error="Invalid item_type"), 400
    if not base_name:
        return jsonify(ok=False, error="Missing base_name"), 400

    df = load_items()
    if df is None or df.empty:
        return jsonify(ok=False, error="No data loaded"), 500

    d = df.copy()
    for col in ("name","category","type","source_table","level","rarity","price_text","Bulk","Source","tags"):
        if col not in d.columns:
            d[col] = ""
    d["name"]        = d["name"].astype(str).str.strip()
    d["category_lc"] = d["category"].astype(str).str.strip().str.lower()
    d["itype"]       = d["type"].astype(str).str.strip().str.lower()
    d["source_lc"]   = d["source_table"].astype(str).str.strip().str.lower()
    d["level"]       = pd.to_numeric(d["level"], errors="coerce").fillna(0).astype(int)

    # Narrow to proper base pool (prefer exact table; fallback to group)
    if t == "weapon":
        pool = _filter_by_sources(d, ["weapon_basic"], "weapons")
        pool = pool[pool["category_lc"].str.contains("weapon", na=False)]

    elif t == "armor":
        pool = _filter_by_sources(d, ["armor_basic"], "armor")
        pool = pool[pool["category_lc"].str.contains("armor", na=False) & ~pool["category_lc"].str.contains("shield", na=False)]

    else:  # shield
        pool = _filter_by_sources(d, ["shield_basic"], "shields" if "shields" in ST_GROUPS else "armor")
        if not pool["source_table"].str.contains("shield_basic", case=False, na=False).any():
            # tighten only when we had to fall back
            is_shield = (
                pool["category_lc"].str.contains("shield", na=False)
                | pool["itype"].str.contains("shield", na=False)
                | pool["name"].str.lower().str.contains("shield", na=False)
            )
            pool = pool[is_shield]

    # Find base row by name (case-insensitive)
    cand = pool[pool["name"].str.casefold() == base_name.casefold()]
    if cand.empty:
        # fallback: contains
        cand = pool[pool["name"].str.lower().str.contains(base_name.lower(), na=False)]
    if cand.empty:
        return jsonify(ok=False, error=f"Base '{base_name}' not found for type '{t}'"), 404

    base = cand.iloc[0]

    # Seed item dict with base info (keep original category/type so appliers see the right thing)
    item = {
        "name": base["name"],
        "level": int(base["level"] or 0),
        "rarity": (str(base["rarity"] or "Common")).title(),
        "price_text": base.get("price_text") or "",
        "price": base.get("price_text") or "",
        "category": base.get("category") or ("Shield" if t=="shield" else t.title()),
        "type": base.get("type") or "",
        "Bulk": base.get("Bulk"),
        "Source": base.get("Source"),
        "tags": base.get("tags"),
        "_base_name": base["name"],
    }

    # Apply runes (pipeline)
    nonce = int(data.get("reroll") or 0)
    seed = f"{t}|{base['name']}|{L}|{nonce}" if nonce else f"{t}|{base['name']}|{L}"
    rng = random.Random(seed)

    rune_cfg = _runes_cfg_for_request(t)  # still only boosts fundamental apply_rate for this tool
    runes_df = _load_runes_df()

    if t == "weapon":
        item = apply_weapon_runes(item, player_level=L, runes_df=runes_df, rng=rng, rune_cfg=rune_cfg)
        composed = _compose_weapon_name(item)
    elif t == "armor":
        item = apply_armor_runes(item, player_level=L, runes_df=runes_df, rng=rng, rune_cfg=rune_cfg)
        composed = _compose_armor_name(item)
    else:
        item = apply_shield_runes(item, player_level=L, runes_df=runes_df, rng=rng, rune_cfg=rune_cfg)
        composed = _compose_armor_name(item)
        
    # Guarantee labels if applier returned none (baseline PF2 thresholds)
    def _weapon_labels(L):
        fund = "+3" if L >= 16 else "+2" if L >= 10 else "+1" if L >= 2 else None
        props = ["Major Striking"] if L >= 19 else ["Greater Striking"] if L >= 12 else ["Striking"] if L >= 4 else []
        return fund, props

    def _armor_labels(L):
        fund = "+3" if L >= 18 else "+2" if L >= 11 else "+1" if L >= 5 else None
        props = ["Major Resilient"] if L >= 20 else ["Greater Resilient"] if L >= 14 else ["Resilient"] if L >= 8 else []
        return fund, props

    if not item.get("_rune_fund_label") and not item.get("_rune_prop_labels"):
        if t == "weapon":
            f, p = _weapon_labels(L)
        else:
            f, p = _armor_labels(L)
        if f: item["_rune_fund_label"] = f
        if p: item["_rune_prop_labels"] = p

    # Compose final name; if composer didn't reflect labels, prefix manually as fallback
    old_name = item.get("name") or base_name
    if t == "weapon":
        final_name = composed or old_name
        if final_name == old_name:
            if item.get("_rune_fund_label"):
                final_name = f'{item["_rune_fund_label"]} {final_name}'
            if item.get("_rune_prop_labels"):
                # put striking-style first if present
                striking = [x for x in item["_rune_prop_labels"] if "striking" in x.lower()]
                others   = [x for x in item["_rune_prop_labels"] if "striking" not in x.lower()]
                prefix = " ".join(striking + others)
                if prefix: final_name = f"{prefix} {final_name}"
    else:
        final_name = composed or old_name
        if final_name == old_name:
            if item.get("_rune_prop_labels"):
                final_name = f'{item["_rune_prop_labels"][-1]} {final_name}'
            if item.get("_rune_fund_label"):
                final_name = f'{item["_rune_fund_label"]} {final_name}'

    item["name"] = final_name

    # Normalize price fields
    if item.get("price") and not item.get("price_text"):
        item["price_text"] = item["price"]

    # AoN search target (use base name)
    item["aon_target"] = item.get("_base_name") or item.get("name")

    return jsonify(ok=True, item=item)

if __name__ == "__main__":
    # For local development. Render should start the app with gunicorn.
    debug = os.environ.get("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, debug=debug)
