# PF2e Shop Generator (Web)

A modular Flask web app that modernizes your Access-based shop generator with a SQLite backend (view: `v_items_norm`), with a CSV fallback.

## Run locally

```bash
pip install flask pandas
python app.py
# Open http://localhost:7860
```

## Configure
Edit `config.json` to switch between `sqlite` and `csv`, tweak counts, disposition multipliers, critical rates, and level spread.

Prices are calculated in whole copper pieces and displayed using standard PF2e denominations. Combined values such as `2 gp 5 sp 3 cp` are accepted. Merchant disposition has its ordinary meaning: **greedy** increases prices, **fair** leaves them unchanged, and **generous** reduces them. The default multipliers are 1.15, 1.00, and 0.90 respectively and can be changed in `config.json`.

The application validates critical configuration values at startup. Paths in `config.json` are resolved relative to that file. These environment variables override configured paths:

- `LOOTGEN_DB_PATH` — item catalog database
- `LOOTGEN_CSV_PATH` — CSV catalog fallback
- `LOOTGEN_STATE_DB_PATH` — persistent Player View database

## Persistent Player Views

Generated Player Views are saved in `data/player_views.db`. A shared Player View URL always loads its stored snapshot; it never rerolls missing inventory. Use a different **Game / Live View** name for each campaign.

Each game also receives a stable, secret **Live Display** link. An immutable Player View remains on one generated shop, while the Live Display checks the persistent state database every three seconds and automatically switches to the newest committed shop for that game. The live link continues to work across workers and restarts when the state database is on persistent storage.

Snapshots are retained for 365 days with a maximum of 250 snapshots per game by default. Current live snapshots are always protected. Change `player_views.retention_days` and `player_views.max_snapshots_per_channel` in `config.json`; set either value to `0` to disable that limit.

Inspect or clean the state database manually with:

```text
python -m services.player_views stats
python -m services.player_views cleanup --vacuum
```

For Render, attach a persistent disk (for example at `/var/data`) and set:

```text
LOOTGEN_STATE_DB_PATH=/var/data/player_views.db
```

Without a persistent disk, Render can discard shared Player Views during a deploy or service restart. The state database is created automatically on first use.

Use `gunicorn app:app` as the Render start command. Flask debug mode is disabled unless `FLASK_DEBUG=1` is set explicitly.

## Reproducing a shop

Every generated shop displays a generation seed. Use **Recreate Same Seed**, or enter that seed with the same shop type, size, disposition, and party level to reproduce the inventory. Leaving the seed blank creates a new random seed. Random state is isolated per request, so concurrent hosted users do not affect one another's results.

Spellbook generation loads each required magical tradition once per request and performs rank, rarity, duplicate, and theme selection in memory. Multiple books of the same tradition reuse that pool instead of repeatedly querying SQLite.

## Tests

Run the complete standard-library suite with:

```text
python -m unittest discover -s tests -v
```

## Port plan
We will port each VBA routine (`genMundane`, `gen_Weapon`, etc.) into focused Python pickers that filter the `v_items_norm` records precisely, replicate rerolls when no item is found, and honor shop types like Tattooist having tattoos only.
