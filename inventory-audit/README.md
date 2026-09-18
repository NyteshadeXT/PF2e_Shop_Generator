# PF2e equipment inventory audit

- Obsidian notes scanned: **5634**
- Database item rows scanned: **4471**
- Excluded database tables: **Spells**, **Spells_Focus**
- Obsidian-only notes: **1868**
- Database-only rows: **704**
- Matched note/database pairs: **3766**
- Obsidian duplicate keys: **4**
- Database names appearing in multiple tables: **0**
- Notes skipped for invalid metadata: **0**

## Review order

1. Resolve `duplicate_vault_items.csv` and `ambiguous_database_names.csv`.
2. Review close-match suggestions in the two `*_only.csv` files.
3. Resolve `metadata_differences` before treating either repository as authoritative.
4. Use `vault_only.csv` as the reviewed addition queue. It includes a target layout, Vault-derived SQLite values, and any export errors.
5. Use `vault_only_errors.csv` to resolve notes with conflicting basic-item indicators. They are excluded from the layout-specific import files.
6. Use `vault_only_material.csv`, `vault_only_basic.csv`, and `vault_only_worn_items.csv` for layout-specific database imports after review.
