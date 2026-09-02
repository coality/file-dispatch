# file-dispatch

Routes incoming files to destination directories based on the metadata in their
JSON sidecar, using rules from a single config file. Meant to run from **cron**.

Each data file arrives with a `.json` sidecar of the same base name
(`orders-42.csv` + `orders-42.json`). On each run the tool reads the JSON, moves
the data file to the directory chosen by your rules, archives the JSON, and logs
what it did. No rule matches → the file is left in place (logged). Nothing is
ever deleted.

## Requirements

`/bin/sh` and **Python 3.9 or newer** (standard library only — nothing to
install). Tested on CPython 3.9 through 3.12. To spot a file still held open by
its producer, Linux uses `/proc` directly; elsewhere `lsof` is used if present,
but is optional.

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

## Command-line options

Run via `./dispatch.sh` (the launcher) or directly with `python3 dispatch.py`.
Both accept the same arguments:

| Option | Effect |
|--------|--------|
| `CONFIG` (positional) | path to the config file |
| `--config-file FILE` | path to the config file (takes priority over the positional) |
| `--dry-run`, `-n` | log what **would** happen but move nothing; every log line is prefixed `DRY-RUN` |
| `--debug`, `-d` | verbose trace: JSON field values, resolved variables, rule resolution atom by atom |
| `--check` | validate the config and exit (`0` = OK); moves nothing |
| `-h`, `--help` | show usage and exit |

`--check`, `--dry-run`, and `--debug` can be combined (e.g. `--dry-run --debug`).

`--dry-run` also checks each destination it resolves: that the directory exists
and is writable, or that it could be created (the closest existing parent is
writable). It checks `JSON_ARCHIVE_DIR` too, which a dry run never creates. A
destination that a real run could not write to is reported as
`would FAIL ... reason='...'` and counted in the `errors=` summary, instead of
being announced as a move that would in fact fail under cron. Nothing is
created: the check is read-only.

### Config file resolution (first one wins)

1. `--config-file FILE`
2. the positional `CONFIG` argument
3. `$DISPATCH_CONFIG` environment variable
4. `dispatch.conf` next to the script

### Python interpreter resolution (first one wins)

1. the config's `PYTHON` setting (the program re-executes itself with it)
2. `$DISPATCH_PYTHON` environment variable
3. `python3` from `PATH`

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | success — a normal run, `--check` passed, or another instance already holds the lock |
| `1` | runtime failure (e.g. the lock file cannot be opened) |
| `2` | the config has errors (parsing/validation failed) |
| `3` | the Python interpreter is missing or not usable |

### Environment variables

| Variable | Effect |
|----------|--------|
| `DISPATCH_CONFIG` | config path, used when no `--config-file`/positional path is given |
| `DISPATCH_PYTHON` | interpreter the launcher uses, unless the config's `PYTHON` overrides it |

## Configuration

`dispatch.conf` has three parts: **settings**, optional **variables**, and
**rules**. `#` starts a comment. See
[`dispatch.conf.example`](dispatch.conf.example) for a fully commented template
with a large cookbook of rule and variable examples.

### Settings

| Setting | Required | Default | Meaning |
|---------|:---:|:---:|---------|
| `INCOMING_DIR` | ✅ | — | directory to watch for `<base>` + `<base>.json` pairs |
| `JSON_ARCHIVE_DIR` | ✅ | — | where each processed `.json` is archived (flat directory) |
| `LOG_DIR` | ✅ | — | holds `dispatch.log`, `errors.log`, and the `.dispatch.lock` lock file |
| `STABLE_SECONDS` | | `2` | a file must stay unchanged this many seconds before it is processed (non-negative integer) |
| `REQUIRED` | | — | comma-separated `$field`s that must be present **and** non-empty, else the file is left in place with an error |
| `PYTHON` | | — | path to the Python 3 interpreter to run the engine with |

```ini
INCOMING_DIR     = "/data/incoming"      # directory to watch
JSON_ARCHIVE_DIR = "/data/archive/json"  # where processed .json files go
LOG_DIR          = "/data/logs"          # holds dispatch.log + errors.log
STABLE_SECONDS   = 2                     # optional: wait for I/O to settle
REQUIRED         = $category, $group     # optional: fields that must be present
# PYTHON         = "/usr/bin/python3"    # optional: interpreter to use
```

### Variables

**`$` means "the value of"** (like a shell): every JSON field is `$field`; define
your own value with `NAME = ...` and reuse it as `$NAME`. Definitions are
evaluated top to bottom, so a variable may reuse the ones above it. Quote
values/paths that contain spaces or commas; a simple value can be quoted or not.
Adjacent pieces concatenate, and `+` joins them explicitly:

```ini
OUT     = "/data/out"
GROUP   = "$group"
REPORTS = "$OUT/Annual Reports"        # quotes -> spaces are fine; $ still expands
TARGET  = $group"/"$category           # concatenation -> "<group>/<category>"
TARGET  = $group + "/" + $category     # the same, written with "+"
```

`+` works anywhere a **value** is expected: variable definitions, both branches
of a ternary, a rule destination, and the right-hand side of a condition. It
absorbs the spaces around it, so `$a + "/" + $b` and `$a"/"$b` are the same
string. Outside quotes `+` is always the operator -- write `"+"` to get a
literal plus. The left-hand side of a condition stays a bare `$field`: no
concatenation and no function call there.

**Functions** available anywhere a value is expected (rules included):

| Function | Example | Result |
|----------|---------|--------|
| `int(...)` | `int($id)` on `"007"` | `"7"` (non-numbers are left unchanged) |
| `upper(...)` | `upper($code)` on `"abc"` | `"ABC"` |
| `lower(...)` | `lower($code)` on `"ABC"` | `"abc"` |

**Ternaries** — a value may be a Python-style `A if <condition> else B`, chainable
like `if`/`elif`/`else`. The `<condition>` uses the **same operators as rules**
(see below); branches may be literals, `$fields`, functions, or concatenations:

```ini
BU      = "core" if $category = "central" else $category
TIER    = "gold" if $vip = "yes" else "silver" if $amount = "high" else "std"
SIZEDIR = "xl" if $bytes >= "1000000" else "l" if $bytes >= "1000" else "s"
CHANNEL = "billing" if $type IN ("invoice", "credit", "debit") else "general"
OWNER   = $owner if $owner != "" else "unassigned"      # fill-in-a-default
```

### Rules

A rule is `condition => "destination directory"`. The **first** rule that matches
wins; if none match, the file is left in place (logged).

| Rule | JSON | Goes to |
|------|------|---------|
| `$category = "report" => "$OUT/$GROUP/reports"` | `{"category":"report","group":"B"}` | `/data/out/B/reports/` |
| `$category IN ("invoice","credit") => "$OUT/billing"` | `{"category":"credit"}` | `/data/out/billing/` |
| `$name = "invoice*" => "$OUT/inv"` | `{"name":"invoice_9"}` | `/data/out/inv/` |
| `$amount >= "10000" => "$OUT/large"` | `{"amount":"25000"}` | `/data/out/large/` |
| `$filename STARTSWITH "INV-" => "$OUT/inv"` | `{"filename":"INV-9"}` | `/data/out/inv/` |
| `$type = "export" AND $status IN ("new","retry") => "$OUT/exports"` | `{"type":"export","status":"new"}` | `/data/out/exports/` |
| `$a = "x" OR $b = "y" => "$OUT/z"` | `{"b":"y"}` | `/data/out/z/` |

**Operators**

| Operator | Meaning |
|----------|---------|
| `=` / `==` | string equality (`*` is a wildcard: `"invoice*"`, `"*-eu"`, `"*"` = any value) |
| `!=` / `<>` | string inequality (wildcards apply here too) |
| `<` `>` `<=` `>=` | numeric comparison — both sides parsed as numbers; a non-numeric value never matches |
| `STARTSWITH` / `ENDSWITH` / `CONTAINS` | plain (literal) substring tests |
| `IN ("a", "b", ...)` | membership; items may be wildcards |
| `AND` / `OR` | combine conditions |
| `( ... )` | grouping |

Precedence is **`( )` > `AND` > `OR`**. The right-hand side of a comparison may
itself be a `$field` or a function, e.g. `$owner = $group` or
`$name = upper($code)`. Destinations are value expressions too, so they can use
`$fields`, variables, and functions.

```ini
# Grouping overrides AND-before-OR:
($category = "order" OR $category = "refund") AND $region = "EU" => "$OUT/eu"

# Quotes let values and destinations contain spaces/commas:
$status IN ("in progress", "on hold") => "$OUT/$GROUP/pending files"

# A catch-all fallback (keep it LAST — first match wins):
$kind = "*" => "$OUT/_unsorted"
```

To add routing for a new file type, add one rule line and re-run `--check`. No
code changes.

## Logs

`LOG_DIR` holds `dispatch.log` (everything) and `errors.log` (warnings/errors
only). Each outcome is one structured line:

```
[INFO]  SUCCESS source='…/orders-42.csv' dest='/data/out/B/orders' target='…/orders-42.csv' (rule #12: …) archived='…/orders-42.json'
[ERROR] FAILURE source='…/bad.xml' dest='…' reason='move failed' - left in place
```

Every run ends with a summary line:

```
[INFO] run summary: processed=… unmatched=… invalid=… incomplete=… unstable=… errors=…
```

`--debug` adds a trace of field values, variables, and rule resolution.

## Security

Incoming files are untrusted; the config is trusted. The tool never runs the
config or JSON as code (a value like `$(cmd)` stays literal), rejects
destinations containing `..`, refuses symlinked data files, and strips control
characters from logs. Runs are serialized by an exclusive `flock` on
`LOG_DIR/.dispatch.lock`; if another instance is already running, the new one
logs a notice and exits `0`.

## Files & tests

- `dispatch.sh` — POSIX-sh launcher (picks the Python interpreter).
- `dispatch.py` — the program: CLI, config resolution, locking, scanning,
  pairing, moving, logging.
- `engine.py` — pure library: config parsing, rule grammar, variable expansion,
  JSON matching.

```sh
python3 -m unittest discover -s tests
```

## License

[MIT](LICENSE)
