# file-dispatch

Routes incoming files to destination directories based on the metadata in their
JSON sidecar, using rules from a single config file. Meant to run from **cron**.

Each data file arrives with a `.json` sidecar of the same base name
(`orders-42.csv` + `orders-42.json`). On each run the tool reads the JSON, moves
the data file to the directory chosen by your rules, archives the JSON, and logs
what it did. No rule matches → the file is left in place (logged). Nothing is
ever deleted.

## Requirements

`/bin/sh` and **python3** (standard library only — nothing to install).
`lsof` is used if present, but is optional.

## Quick start

```sh
cp dispatch.conf.example dispatch.conf
$EDITOR dispatch.conf
./dispatch.sh --check      # validate the config
./dispatch.sh --dry-run    # preview, move nothing
./dispatch.sh              # run for real
```

Cron (every 2 minutes, overlap-safe):

```cron
*/2 * * * * /path/to/dispatch.sh --config-file /path/to/dispatch.conf >> /path/to/logs/cron.log 2>&1
```

## Config

`dispatch.conf` has settings, optional variables, and rules. `#` starts a
comment. See [`dispatch.conf.example`](dispatch.conf.example) for a commented
template.

```ini
INCOMING_DIR     = "/data/incoming"      # directory to watch
JSON_ARCHIVE_DIR = "/data/archive/json"  # where processed .json files go
LOG_DIR          = "/data/logs"          # holds dispatch.log + errors.log
STABLE_SECONDS   = 2                     # optional: wait for I/O to settle
REQUIRED         = $category, $group     # optional: fields that must be present
# PYTHON         = "/usr/bin/python3"    # optional: interpreter to use
```

**`$` means "the value of"** (like a shell): every JSON field is `$field`; define
your own with `NAME = ...` and reuse it as `$NAME`. Quote values/paths that
contain spaces or commas; a simple value can be quoted or not.

Variables build paths (top to bottom, adjacent pieces concatenate):

```ini
OUT   = "/data/out"
GROUP = "$group"
```

Rules — `condition => "destination directory"`, **first match wins**:

| Rule | JSON | Goes to |
|------|------|---------|
| `$category = "report" => "$OUT/$GROUP/reports"` | `{"category":"report","group":"B"}` | `/data/out/B/reports/` |
| `$category IN ("invoice","credit") => "$OUT/billing"` | `{"category":"credit"}` | `/data/out/billing/` |
| `$name = "invoice*" => "$OUT/inv"` | `{"name":"invoice_9"}` | `/data/out/inv/` |
| `$type = "export" AND $status IN ("new","retry") => "$OUT/exports"` | `{"type":"export","status":"new"}` | `/data/out/exports/` |
| `$a = "x" OR $b = "y" => "$OUT/z"` | `{"b":"y"}` | `/data/out/z/` |

- operators: `=`, `IN (...)`, `AND`, `OR` (**AND before OR**); `*` is a wildcard.
- To add routing for a new file type, add one rule line and re-run `--check`. No
  code changes.

## Logs

`LOG_DIR` holds `dispatch.log` (everything) and `errors.log` (warnings/errors).
Each outcome is one line:

```
[INFO]  SUCCESS source='…/orders-42.csv' dest='/data/out/B/orders' target='…/orders-42.csv' (rule #12: …) archived='…/orders-42.json'
[ERROR] FAILURE source='…/bad.xml' dest='…' reason='move failed' - left in place
```

`--debug` adds a trace of field values, variables, and rule resolution.

## Options

| Option | Effect |
|--------|--------|
| `--config-file FILE` | config path (else `$DISPATCH_CONFIG`, else `dispatch.conf` next to the script) |
| `--dry-run`, `-n` | log what would happen, move nothing |
| `--debug`, `-d` | verbose trace |
| `--check` | validate the config and exit |

## Files & tests

- `dispatch.sh` — launcher (picks the Python interpreter).
- `dispatch.py` — the program; `engine.py` — config parsing and rule matching.

```sh
python3 -m unittest discover -s tests
```

## Security

Incoming files are untrusted; the config is trusted. The tool never runs the
config or JSON as code (a value like `$(cmd)` stays literal), rejects
destinations containing `..`, refuses symlinked data files, and strips control
characters from logs.

## License

[MIT](LICENSE)
