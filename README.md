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
| `--dry-run`, `-n` | move nothing, but log exactly the lines a real run would write; each is prefixed `DRY-RUN` |
| `--debug`, `-d` | verbose trace: JSON field values, resolved variables, rule resolution atom by atom |
| `--check` | validate the config and exit (`0` = OK); moves nothing |
| `-h`, `--help` | show usage and exit |

`--check`, `--dry-run`, and `--debug` can be combined (e.g. `--dry-run --debug`).

`--dry-run` also checks each destination it resolves: that the directory exists
and is writable, and — only when `CREATE_DIRS = yes` — that a missing one could
be created (the closest existing parent is writable). It checks
`JSON_ARCHIVE_DIR` too, which a dry run never creates. A
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
| `CREATE_DIRS` | | `no` | `yes` creates a missing destination directory; `no` treats it as an error and leaves the file in place |
| `DISPATCH_WITHOUT_JSON` | | `no` | `yes` also dispatches a data file that has no `.json` sidecar, on its system metadata alone |
| `REQUIRED` | | — | comma-separated `$field`s that must be present **and** non-empty, else the file is left in place with an error; an explicit `null` counts as present |
| `PYTHON` | | — | path to the Python 3 interpreter to run the engine with |

```ini
INCOMING_DIR     = "/data/incoming"      # directory to watch
JSON_ARCHIVE_DIR = "/data/archive/json"  # where processed .json files go
LOG_DIR          = "/data/logs"          # holds dispatch.log + errors.log
STABLE_SECONDS   = 2                     # optional: wait for I/O to settle
CREATE_DIRS      = no                    # optional: create missing destinations?
DISPATCH_WITHOUT_JSON = no               # optional: handle files with no sidecar?
REQUIRED         = $category, $group     # optional: fields that must be present
# PYTHON         = "/usr/bin/python3"    # optional: interpreter to use
```

### System metadata

Three fields come from the filesystem, not from the sidecar, and are available
to every rule and variable:

| Field | Value | Example |
|-------|-------|---------|
| `$Filename` | base name, extension included | `orders-42.csv` |
| `$Filesize` | size in bytes, as digits — the numeric operators work on it | `10485760` |
| `$Filedatetime` | mtime, local time, `YYYY-MM-DDTHH:MM:SS` | `2026-09-02T13:31:20` |

```ini
$Filename ENDSWITH ".csv"      => "$OUT/csv"
$Filesize > "10000000"         => "$OUT/large"
$Filedatetime STARTSWITH "2026-09" => "$OUT/2026-09"
```

A sidecar field of the same name **wins**: the producer's metadata is the
authority, these only fill in what it does not say. `--debug` labels each field
`json field:` or `system field:` so you can see which is which.

### Files with no sidecar

By default a data file with no `<base>.json` is not ready — the producer
announces a file by writing both halves — so it waits for a later run and is
counted in `incomplete=`. With `DISPATCH_WITHOUT_JSON = yes` it becomes a unit
of work of its own: rules see only the three system fields (every other
`$field` is empty), and since there is no sidecar, the log ends `archived='-'`.

```ini
DISPATCH_WITHOUT_JSON = yes
$Filename ENDSWITH ".csv"  => "$OUT/csv"
```

⚠️ **The producer's ordering matters.** If it writes the data file first and its
sidecar a moment later, turning this on means the data file can be dispatched
before the sidecar arrives — leaving the sidecar orphaned in `INCOMING_DIR`.
Turn it on only when files genuinely arrive alone, or raise `STABLE_SECONDS`
past the gap between the two writes.

Note that `$field = "*"` matches an **absent or empty** field too, so it is not
a test for "the sidecar provided this". Use `$field != ""` for that.

### Creating destinations

By default (`CREATE_DIRS = no`) destination directories are **not** created: a
rule that resolves to a directory which does not exist is an error, the file is
left in place, and the run reports it.

```
[ERROR] FAILURE source='…/a.xml' dest='/data/out/absent'
        reason='destination directory does not exist (CREATE_DIRS is no)' - left in place
```

That is the safe default: a typo in a rule, or a field that came through empty,
otherwise silently builds a new tree somewhere instead of failing loudly. Set
`CREATE_DIRS = yes` to have missing destinations created with `makedirs`.
`--dry-run` follows the same setting, so it reports what a real run would do.

`LOG_DIR` and `JSON_ARCHIVE_DIR` are the tool's own directories, not routing
targets, and are still created regardless.

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
TEAM    = "core" if $unit = "central" else $unit
TIER    = "gold" if $vip = "yes" else "silver" if $amount = "high" else "std"
SIZEDIR = "xl" if $bytes >= "1000000" else "l" if $bytes >= "1000" else "s"
CHANNEL = "billing" if $type IN ("invoice", "credit", "debit") else "general"
OWNER   = $owner if $owner != "" else "unassigned"      # fill-in-a-default
```

**The `else` is optional.** Written without one, an assignment only fires when
its condition holds; otherwise the variable **keeps the value it already had**.
Several lines about the same variable then read like `if`/`elif`, instead of the
last one always winning:

```ini
ST  = upper($status)                        # normalize once, test many times
APP = "reports"  if $ST CONTAINS "REPORT"   # first match sets it ...
APP = "invoices" if $ST CONTAINS "INVOICE"  # ... a later one overrides
```

If no line matches, the variable holds `""` — or whatever it was set to earlier,
which is the easy way to give it a default:

```ini
STAGE = "dev"                               # default, then refine
STAGE = "staging" if $env = "stage"
STAGE = "prod"    if $env = "production"
```

An empty variable is not an error: it simply leaves an empty segment in the
destination (`/out//x`). Seed a default, or end the chain with a plain `else`,
whenever an empty value would build a path you don't want. An `if` with nothing
after it (`X = "a" if`) is rejected by `--check`.

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
| `ISNULL` / `ISNOTNULL` | the field was present in the JSON as `null` (takes no right-hand side) |
| `AND` / `OR` | combine conditions |
| `( ... )` | grouping |

### null fields

JSON `null` has no string form, so as a *value* a null field reads as `""`, the
same as an empty or absent one. Where it differs:

- **`REQUIRED` accepts it.** `"status": null` is the producer answering "no
  value here", which is an answer. A field that is absent, or set to `""`, still
  fails.
- **`ISNULL` / `ISNOTNULL` test it**, in rules and in ternary conditions alike.
  Only a real JSON `null` is null: `""` and an absent field are not.

```ini
REQUIRED = $category, $status                # passes on "status": null

KIND = "none" if $status ISNULL else lower($status)

$status ISNULL    => $OUT + "/unset"
$status ISNOTNULL => $OUT + "/set/" + $KIND
```

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
only). Each outcome is one structured line, `<STATUS> <action>` followed by
`key='value'` fields:

```
[INFO]  SUCCESS move source='…/orders-42.csv' dest='/data/out/B/orders' target='…/orders-42.csv' (rule #12: …) archived='…/orders-42.json'
[ERROR] FAILURE move source='…/bad.xml' dest='…' reason='move failed' (rule #12: …) - left in place
[ERROR] FAILURE archive source='…/x.csv' target='…' reason='data moved but JSON archiving failed'
```

**A dry run writes the same lines**, with `DRY-RUN` inserted after the level —
same status, same action, same fields. So a `--dry-run` log is a prediction of
the real one, and the two can be diffed:

```
[INFO]  DRY-RUN SUCCESS move source='…' dest='…' target='…' (rule #12: …) archived='…'
[INFO]          SUCCESS move source='…' dest='…' target='…' (rule #12: …) archived='…'
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
