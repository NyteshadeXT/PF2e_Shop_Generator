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

Generating another shop for an existing game now saves it as a **draft** without changing what players see. Use **Open Player View** to inspect or share that shop's permanent link, then choose **Publish to Live Display** when it is ready for the shared player-facing screen. The first shop generated for a new game becomes its initial live shop automatically. Drafts are retained in **Recent Shops**, where they can also be published later with **Make Live**.

Generation uses a redirect-safe results workflow. Refreshing a results page only reloads the stored shop, and repeated submissions of the same browser request resolve to that same immutable snapshot instead of creating duplicate drafts. This idempotency key is enforced in persistent storage, so it also works when repeated requests reach different Gunicorn workers. Recent Shops can reopen either the full GM results or the player-facing view without regenerating inventory.

Live Display polling pauses completely when its browser tab is hidden, resumes immediately when the tab becomes visible, and backs off to a maximum of 30 seconds during network interruptions. Conditional version checks reduce unchanged response traffic. A rotated link stops polling and tells the player to request the new URL instead of retrying indefinitely.

Player View storage uses SQLite write-ahead logging so player screens can continue reading while the GM publishes a shop. Schema checks and migrations run once per database file and worker instead of during every Live Display poll, reducing persistent-disk traffic on Render.

Use **Recent Shops** from the generator to recover Player View links after closing a tab. The screen can filter by game name, reopen any retained immutable shop, and deliberately make an older shop current again if the wrong inventory was advanced to a Live Display. Restoring a shop keeps the same secret Live Display URL, so player bookmarks and shared links continue working.

Recent Shops provides a picker containing every known game and its retained shop count. History is paginated in groups of 50, so all retained snapshots remain reachable even when a game uses the default 250-shop limit.

Snapshots are retained for 365 days with a maximum of 250 snapshots per game by default. Current live snapshots are always protected. Change `player_views.retention_days` and `player_views.max_snapshots_per_channel` in `config.json`; set either value to `0` to disable that limit.

Inspect or clean the state database manually with:

```text
python -m services.player_views stats
python -m services.player_views cleanup --vacuum
python -m services.player_views backup --output backups/player_views.db
```

The backup command uses SQLite’s online backup operation and checks the completed copy before replacing an older backup at that destination. It is safe to run while the web service is active. Keep backups outside an ephemeral service filesystem.

The GM-facing **Recent Shops** page also provides **Download Backup**. It creates the same online, integrity-checked SQLite backup and sends it directly to the browser, which is more convenient for GitHub-to-Render deployments. The downloaded file contains all retained snapshots and secret Live Display links, so store it privately outside Render. When GM access is enabled, the download requires an authenticated GM session and a valid browser request token.

To recover from a downloaded backup, stop the web service before replacing the persistent `player_views.db`, then restart it. Do not overwrite the active database while the application is running. Keep the original file until the restored service passes `/health` and the Recent Shops page has been checked.

For Render, attach a persistent disk (for example at `/var/data`) and set:

```text
LOOTGEN_STATE_DB_PATH=/var/data/player_views.db
```

Without a persistent disk, Render can discard shared Player Views during a deploy or service restart. The state database is created automatically on first use.

GitHub stores and deploys the application source and catalog, but it does not contain Render’s live `player_views.db` because that file is intentionally ignored by Git. A GitHub-triggered deployment therefore still requires a Render persistent disk and a separate backup destination for runtime Player Views.

The repository must include both `config.json` and `data/PF2e_Treasure_Generator_Backend.db`. They are application assets, not Render secrets. CI verifies that both files exist, that the configuration points to the catalog, and that the catalog passes SQLite integrity and required-view checks. Only `data/player_views.db` and its WAL sidecars should remain ignored as runtime data.

Use `gunicorn app:app` as the Render start command. Flask debug mode is disabled unless `FLASK_DEBUG=1` is set explicitly.

### Optional GM access for a hosted generator

To keep the generator controls private on Render, add these secret environment variables:

```text
LOOTGEN_GM_ACCESS_KEY=<a long private passphrase>
LOOTGEN_SESSION_SECRET=<a different long random value>
```

When `LOOTGEN_GM_ACCESS_KEY` is set, the generator and its GM tools require the access key. The **Lock Generator** button ends the 12-hour GM session early. Immutable Player View links, secret Live Display links, and `/health` remain public, so players can continue using shared links without the GM key and everyone opening the same link sees the same stored shop. If the access key is unset, the application behaves as before with no login screen.

Failed GM logins are limited to eight attempts per client in five minutes by default. Adjust these only if necessary with `LOOTGEN_LOGIN_ATTEMPTS` and `LOOTGEN_LOGIN_WINDOW_SECONDS`. A successful login clears that client’s failures.

Render automatically receives secure session cookies. For another HTTPS host, set `LOOTGEN_SECURE_COOKIES=1`. Store all of these values as host environment secrets rather than putting them in `config.json` or source control.

Shop generation accepts POST requests only. This prevents a link preview, crawler, or casually opened URL from generating a shop and advancing a live game channel.

State-changing browser forms also require a session-specific request token. Requests copied from another site cannot generate a shop, change the current Live Display, log in, or log the GM out. Player View URLs are read-only GET endpoints.

If a secret Live Display URL is exposed, use **Recent Shops → Rotate Live Link** for that game. The current shop remains live under a new secret URL, and every older Live Display link for that game immediately stops working. Immutable Player View links are unaffected.

`/health` verifies both the item catalog and Player View storage and returns HTTP 503 if either is unavailable, making it suitable for a Render health-check path. Detailed `/debug/*` routes are disabled by default; enable them temporarily with `LOOTGEN_ENABLE_DEBUG_ROUTES=1` only in a trusted environment.

Incoming request bodies are limited to 2 MiB by default to protect the hosted service from accidental oversized submissions. Override this only when needed with `LOOTGEN_MAX_REQUEST_BYTES`.

Browser errors use a concise generator-styled page, while `/api/*` errors remain JSON for the built-in tools. Routine loading and selection details use structured application logging instead of direct console output; expected health-check failures are reported without repeated stack traces.

Database text is escaped before trusted spellbook markup is rendered, and the Magic Item Builder inserts API values through safe DOM text nodes. Responses also suppress referrer data so secret Live Display URLs are not disclosed when players follow external links.

Browser responses use a restrictive Content Security Policy: executable scripts require a per-request nonce, resources and API connections default to the application’s own origin, objects and base-tag rewriting are disabled, and forms may submit only to the application. HTTPS responses also enable one-year Strict Transport Security. Existing inline layout styles remain allowed until the templates are fully moved to stylesheet classes.

## Reproducing a shop

Every generated shop displays a generation seed, a build fingerprint, and **Copy Reproduction Key**. A seed reproduces inventory when used with the same shop type, size, disposition, and party level. A reproduction key carries those values plus the fingerprint of the generator code, configuration, catalog, Python, pandas, and NumPy versions. When an older or mismatched key is used, its settings are still restored but the results page warns that exact inventory may differ. Existing `pf2e1` keys remain supported. **Recreate Same Seed** remains the quickest one-click option from the results page. Leaving the field blank creates a new random seed. Random state is isolated per request, so concurrent hosted users do not affect one another's results.

CSV export has been removed in favor of reproducible shops and immutable Player View links.

Spellbook generation loads each required magical tradition once per request and performs rank, rarity, duplicate, and theme selection in memory. Multiple books of the same tradition reuse that pool instead of repeatedly querying SQLite.

Spell and formula reference tables are shared through a thread-safe process cache. Scrolls, wands, generated-shop spellbooks, and the standalone spellbook tool reuse the same spell data. The cache automatically refreshes when the catalog database file changes, and concurrent first requests perform only one reference query.

## Tests

Run the complete standard-library suite with:

```text
python -m unittest discover -s tests -v
```

GitHub Actions runs the same suite on every push and pull request using `.github/workflows/tests.yml`. In Render, enable the option to wait for CI checks before auto-deploying so a failed GitHub test run does not immediately replace the working service.

After the regression suite, GitHub Actions now starts the application with the production two-worker Gunicorn configuration and a temporary Player View database. It performs real HTTP checks against `/health`, GM authentication routing, the primary stylesheet, a missing public Player View, Content Security Policy, and Render-mode HSTS. A deployment therefore cannot pass CI when the application imports successfully but fails to operate through the production server.

## Port plan
We will port each VBA routine (`genMundane`, `gen_Weapon`, etc.) into focused Python pickers that filter the `v_items_norm` records precisely, replicate rerolls when no item is found, and honor shop types like Tattooist having tattoos only.
