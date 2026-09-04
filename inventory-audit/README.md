# PF2e equipment inventory audit

- Obsidian notes scanned: **3490**
- Database item rows scanned: **4503**
- Excluded database tables: **Spells**, **Spells_Focus**
- Obsidian-only notes: **1351**
- Database-only rows: **2357**
- Matched note/database pairs: **2148**
- Obsidian duplicate keys: **6**
- Database names appearing in multiple tables: **18**
- Notes skipped for invalid metadata: **2059**

## Review order

1. Resolve `duplicate_vault_items.csv` and `ambiguous_database_names.csv`.
2. Review close-match suggestions in the two `*_only.csv` files.
3. Resolve `metadata_differences` before treating either repository as authoritative.
4. After cleanup, use `vault_only.csv` as the proposed database-addition queue.
