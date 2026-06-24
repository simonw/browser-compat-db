# Feedback on sqlite-utils 4.0rc1

Collected while upgrading this project (`browser-compare-db`) from
`sqlite-utils>=3.39` to `sqlite-utils>=4.0rc1`. Tested version: **4.0rc1**
(`sqlite_utils-4.0rc1`, sdist uploaded 2026-06-21), Python 3.11.

Overall the upgrade was small. One cosmetic change (identifier quoting) and one
behavioural change in transaction handling that surfaced a real, if narrow,
`VACUUM` failure. Details and minimal repros below.

---

## 1. Transaction handling change → `cannot VACUUM from within a transaction`

**This is the one worth a look before 4.0 final.**

### What we hit

Our build script does a batch of `upsert_all` / `delete_where` syncs and then
finishes with `db.vacuum()`. Under 3.x this worked for years. Under 4.0rc1 it
fails with:

```
sqlite3.OperationalError: cannot VACUUM from within a transaction
```

…but only sometimes — specifically when one of the synced tables ends up empty
and we take a `table.delete_where()` (delete-all) path as the last write before
`vacuum()`.

### Root cause

Two facts combine:

1. **`delete_where()` leaves an uncommitted transaction open.** This is true in
   *both* 3.39 and 4.0rc1, and is asymmetric with `insert_all`/`upsert_all`,
   which leave the connection with no open transaction. (See issue #3 below.)

2. **In 4.0, a subsequent write no longer flushes that pre-existing
   transaction.** In 3.x, the next `insert`/`upsert` effectively committed the
   dangling transaction, so by the time `vacuum()` ran the connection was clean.
   In 4.0 the dangling transaction persists, and `VACUUM` (which cannot run
   inside a transaction) raises.

### Minimal repro

```python
import sqlite_utils
db = sqlite_utils.Database(memory=True)
db["t"].insert_all([{"id": 1}, {"id": 2}], pk="id")
print("after insert_all   in_transaction =", db.conn.in_transaction)
db["t"].delete_where()                      # delete-all
print("after delete_where in_transaction =", db.conn.in_transaction)
db["meta"].insert({"k": "v"})               # a later, unrelated write
print("after later insert in_transaction =", db.conn.in_transaction)
db.vacuum()
```

Output:

| step                            | 3.39    | 4.0rc1  |
| ------------------------------- | ------- | ------- |
| after `insert_all`              | `False` | `False` |
| after `delete_where()`          | `True`  | `True`  |
| after later `insert()`          | **`False`** | **`True`** |
| `db.vacuum()`                   | OK      | `OperationalError: cannot VACUUM from within a transaction` |

The single behavioural difference is the third row: in 3.x the later write
flushed the open transaction; in 4.0 it does not.

### Why this matters

`VACUUM`, `ATTACH`, and a few other statements legitimately cannot run inside a
transaction. Any code that mixes `delete_where()` (or any operation that leaves
a transaction open) with a later `vacuum()` and relied on 3.x auto-flushing will
break the same way. The failure is also order/data-dependent (it only triggers
when the open-transaction operation is the last write before `vacuum`), which
makes it easy to ship without noticing — our test suite only caught it because
one fixture happened to empty a table.

### Suggestions (any one of these would have spared us)

- Have `Database.vacuum()` (and similar non-transactional statements) commit any
  open transaction first, or document clearly that the caller must.
- Make `delete_where()` consistent with `insert_all`/`upsert_all` re: leaving the
  connection without an open transaction (see #3).
- If the "writes no longer auto-flush a pre-existing transaction" change is
  intentional (it reads like a deliberate transaction-model rework), call it out
  prominently in the 4.0 upgrade notes — it's a subtle but real semantic change.

### Our workaround

```python
if db.conn.in_transaction:
    db.conn.commit()
db.vacuum()
```

---

## 2. Identifier quoting changed from `[brackets]` to `"double quotes"`

Not a bug — just flagging it because it produces a visible diff for anyone who
snapshots `db.schema` (golden tests, generated docs, etc.).

In 3.x, generated DDL used SQL Server-style bracket quoting; in 4.0 it uses
standard double-quote quoting:

```diff
-CREATE TABLE [browsers] (
-   [id] TEXT PRIMARY KEY,
-   [name] TEXT
+CREATE TABLE "browsers" (
+   "id" TEXT PRIMARY KEY,
+   "name" TEXT
 );
-CREATE INDEX [idx_features_category] ON [features] ([category]);
+CREATE INDEX "idx_features_category" ON "features" ("category");
```

Double quotes are the more standard/portable choice, so this looks like a good
change. It's worth an explicit line in the changelog/upgrade notes, since it
will dirty any committed schema snapshot on first re-run (it did for ours — our
README embeds `db.schema` via cog).

---

## 3. Minor: `delete_where()` is inconsistent with the other write helpers

Independent of the version change, `delete_where()` leaves the connection in an
open transaction, whereas `insert`, `insert_all`, `upsert` and `upsert_all` do
not:

```python
import sqlite_utils
db = sqlite_utils.Database(memory=True)
db["t"].insert_all([{"id": 1}], pk="id")
print(db.conn.in_transaction)   # False
db["t"].delete_where()
print(db.conn.in_transaction)   # True   <- surprising asymmetry
```

This is true in 3.39 as well, so it's long-standing rather than a 4.0
regression. It was harmless in 3.x because the next write flushed it; with 4.0's
transaction changes it's now load-bearing (it's the proximate cause of issue
#1). Making `delete_where()` consistent with the insert/upsert helpers would
remove a sharp edge.

---

## Summary

- **#1 (transaction/VACUUM)** — please consider before 4.0 final; it's a real
  breakage path with a non-obvious trigger.
- **#2 (quoting)** — looks intentional and good; just deserves an upgrade note.
- **#3 (`delete_where` asymmetry)** — minor, but fixing it would also resolve #1.

Happy to file these as separate GitHub issues with the repros above if useful.
