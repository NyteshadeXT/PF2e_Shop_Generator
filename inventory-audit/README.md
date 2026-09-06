# PF2e equipment inventory audit

- Obsidian notes scanned: **5548**
- Database item rows scanned: **4495**
- Excluded database tables: **Spells**, **Spells_Focus**
- Obsidian-only notes: **2006**
- Database-only rows: **953**
- Matched note/database pairs: **3542**
- Obsidian duplicate keys: **0**
- Database names appearing in multiple tables: **0**
- Notes skipped for invalid metadata: **0**

## Review order

1. Resolve `duplicate_vault_items.csv` and `ambiguous_database_names.csv`.
2. Review close-match suggestions in the two `*_only.csv` files.
3. Resolve `metadata_differences` before treating either repository as authoritative.
4. After cleanup, use `vault_only.csv` as the proposed database-addition queue.
