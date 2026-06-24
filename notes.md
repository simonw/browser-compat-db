# Notes on the shape of mdn/browser-compat-data

Working notes captured while building the importer. The authoritative schemas
live in the upstream repo under `schemas/` (`browsers.schema.json` and
`compat-data.schema.json`), with human-readable companions
`browsers-schema.md` and `compat-data-schema.md`.

## Repository layout

The data is plain JSON spread across top-level category directories:

| Directory       | Contents                                    |
| --------------- | ------------------------------------------- |
| `browsers/`     | Browser metadata + release history (17 files) |
| `api/`          | Web API compat data (largest, ~1100 files)  |
| `css/`          | CSS properties, selectors, at-rules, etc.   |
| `html/`         | HTML elements, attributes, global attrs     |
| `http/`         | HTTP headers, methods, status codes         |
| `javascript/`   | Built-ins, operators, statements, grammar   |
| `mathml/`       | MathML elements                             |
| `svg/`          | SVG elements and attributes                 |
| `webassembly/`  | WebAssembly features                        |
| `webdriver/`    | WebDriver / BiDi commands                   |
| `webextensions/`| Browser extension APIs and manifest keys    |
| `mediatypes/`   | Media type (MIME) support                   |

Non-data directories (`scripts/`, `utils/`, `types/`, `schemas/`, ...) are
ignored by the importer. Test fixtures elsewhere in the tree also contain
`__compat` blocks, which is why a naive "grep every JSON file" count is
slightly higher than the importer's feature count — we only walk the category
directories above.

## Browser metadata (`browsers/*.json`)

Each file is `{"browsers": {"<id>": {...}}}`. A browser has:

- `name`, `type` (`desktop` | `mobile` | `server` | `xr`)
- `upstream` (optional) — e.g. `edge` mirrors `chrome`
- `preview_name`, `pref_url`, `accepts_flags`, `accepts_webextensions`
- `releases`: `{"<version>": {release_date, release_notes, status, engine,
  engine_version}}`. `status` is one of `retired`, `current`, `beta`,
  `nightly`, `planned`, `esr`.

## Compat data — the nested tree

Compat files are deeply nested objects. **Any object can carry a `__compat`
key**, and the dotted path of keys leading to it is the feature identifier,
e.g. `css.properties.grid`, and a sub-value `css.properties.grid.none`.
A feature can therefore be the *parent* of other features.

`__compat` contains:

- `description` (optional, may contain HTML)
- `mdn_url` (optional)
- `spec_url` — **string or list of strings** (optional)
- `tags` — list of strings (optional, e.g. `web-features:grid`)
- `status` — `{experimental, standard_track, deprecated}` (booleans)
- `support` — `{"<browser_id>": <support entry>}`

### Support entries (the tricky part)

A browser's support value is one of three shapes:

1. The literal string `"mirror"` — "mirror whatever the upstream browser does"
   (e.g. `chrome_android` mirroring `chrome`). Resolving this fully requires
   the upstream build logic; we store it faithfully as `is_mirror = 1` rather
   than resolving it.
2. A single **simple support statement** object.
3. An **array** mixing strings and objects (multiple support ranges / history).

Across the full dataset the only string value ever seen is `"mirror"`.

A simple support statement has these optional fields:

- `version_added` / `version_removed` — a **version string** (`"57"`, `"≤10"`,
  `"preview"`), a **boolean** (`true` = supported but version unknown,
  `false` = not supported), or **null** (unknown).
- `prefix` (e.g. `-webkit-`), `alternative_name`
- `partial_implementation` (bool)
- `flags` — list of `{type (`preference`|`runtime_flag`), name, value_to_set}`
- `impl_url` — **string or list of strings**
- `notes` — **string or list of strings**

### Modeling choices

- `version_added`/`version_removed` are normalized to
  `(version_string, supported_flag)`: the string column holds versions like
  `"57"`/`"≤10"`/`"preview"`; the `supported` column is `1`/`0`/`NULL` for the
  boolean/unknown cases. This makes both "added in which version" and "is it
  supported at all" easy to query.
- `notes` and `impl_url` lists are joined with newlines into a single column.
- Each statement in a `support` array is stored as its own row keyed by
  `statement_index`; flags reference that composite key.

## Idempotent refresh

`build_db.py` upserts every entity keyed on a stable primary key and then
prunes rows whose key is no longer present in the source, so re-running against
an updated checkout applies additions, edits **and** deletions in place. This
was verified by importing an old commit, then a newer `main`, on the same
database file (feature/release counts went up, `support_flags` went down as
expected, and features removed upstream disappeared).

## Source freshness

The importer records, in a `metadata` key/value table, the upstream
`package.json` version (`bcd_version`), the checked-out git commit
(`data_commit`), that commit's date (`data_last_updated`, read from
`git log -1 --format=%cI`), and when the import ran (`imported_at`).
