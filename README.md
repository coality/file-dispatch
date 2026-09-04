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
| `--no-dry-run` | move files even if the config sets `DRY_RUN = yes` |
| `--debug`, `-d` | verbose trace: JSON field values, resolved variables, rule resolution atom by atom |
| `--no-debug` | stay quiet even if the config sets `DEBUG = yes` |
| `--check` | validate the config and exit (`0` = OK); moves nothing |
| `-h`, `--help` | show usage and exit |

`--check`, `--dry-run`, and `--debug` can be combined (e.g. `--dry-run --debug`).

`--dry-run` and `--debug` also exist as the `DRY_RUN` and `DEBUG` settings, for
turning them on in a config rather than on every invocation. **The command line
wins**: a flag overrides the setting, and the `--no-` twins override it the
other way, so a config left in `DRY_RUN = yes` never silently swallows a real
run. The config is consulted only when neither flag is given.

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
| `DRY_RUN` | | `no` | `yes` behaves like `--dry-run` |
| `DEBUG` | | `no` | `yes` behaves like `--debug` |
| `LOG_MAX_MB` | | `10` | roll a log over once it would exceed this size; `0` disables rotation |
| `LOG_KEEP` | | `5` | how many rolled-over generations to keep (`0` truncates instead) |
| `REPORT_DIR` | | — | write `report.csv` here: one row per file, updated across runs. Unset = no report |
| `REPORT_KEEP_DAYS` | | `90` | drop rows for files no longer around after this many days (`0` keeps everything) |
| `REPORT_SPLIT` | | `none` | `daily` / `monthly` publish one file per period instead of a single `report.csv` |
| `REQUIRED` | | — | comma-separated `$field`s the **sidecar** must provide, non-empty, else the file is left in place with an error; an explicit `null` counts as present, and a file with no sidecar is not checked |
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

`REQUIRED` is not held against such a file: it states what a sidecar must
provide, and there is no sidecar to hold to it. Files that do have one are
still checked as before, so the two settings can be used together.

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

## The report

With `REPORT_DIR` set, each run writes `report.csv` there: **one row per file**,
updated in place run after run. The logs say what happened during one run; this
answers the question asked afterwards — *what became of `orders-42.csv`?*

```csv
filename,first_seen,file_date,destination,moved_at,status,retries,reason
good.csv,2026-09-02T17:33:41.812,2026-09-02T17:33:41,/data/out/ok,2026-09-02T17:33:42,success,0,
late.csv,2026-09-02T17:30:02.119,2026-09-02T17:30:01,/data/out/wait,2026-09-02T17:33:42,success,2,
odd.csv,2026-09-02T17:30:02.140,2026-09-02T17:30:01,,,unmatched,3,no rule matched
stuck.csv,2026-09-02T17:30:02.155,2026-09-02T17:30:01,/data/out/x,,failed,7,check: [Errno 13] destination directory is not writable
```

| Column | Meaning |
|--------|---------|
| `filename` | base name as it arrived in `INCOMING_DIR` |
| `first_seen` | when this run's dispatch first observed the file (milliseconds: it is half the row's identity) |
| `file_date` | the file's own mtime — when the producer finished writing it |
| `destination` | where it went, or where it *should* have gone: filled in even on a failure |
| `moved_at` | when the move succeeded; empty while it has not |
| `status` | `success`, `failed`, `unmatched` (no rule claimed it), `pending` (waiting for its sidecar, or still being written) |
| `retries` | how many further runs have tried since the first attempt |
| `reason` | why it is not `success` — the same wording as the log |

**A failure is never repeated**: the row is updated and `retries` grows, so one
stuck file is one line however long it stays stuck. When it finally goes
through, that same row turns `success` and keeps its retry count as a record of
how long it took.

A row is identified by `filename` **and** `first_seen`, so a name that comes
back later — periodic exports reuse names constantly — opens a new row instead
of being added to the finished one. A success closes a row for good.

`--dry-run` never writes the report. Rows for files that are no longer around
are dropped after `REPORT_KEEP_DAYS`.

### Opening the report while it is in use

`REPORT_DIR` holds two files:

| File | Role |
|------|------|
| `report.state` | the authority. Written first, every run. Nothing else reads it |
| `report.csv` | a copy of it, published for you, Excel and Power BI |

They are separate because of what a spreadsheet does to a file on a network
share: Excel holds it open with an SMB deny-write lock for the whole editing
session, and the rename that publishes a new version is then refused. On Linux
alone this never happens — replacing a file someone is reading works, and the
reader keeps seeing the version it opened — but over SMB it does.

So publishing is allowed to fail. The state is written first and always, the
run's bookkeeping is never lost, `report.csv` simply stays as it was, and a
`WARN` says so:

```
[WARN] report.csv could not be updated (a program is holding it open?) - the state
       is safe in '…/report.state' and it will be published on the next run
```

The next run republishes it, complete. Nothing needs doing beyond closing the
file. It stays a warning rather than an error precisely because it heals
itself.

`report.state` has no `.csv` extension on purpose: a Power BI folder import
filtering `*.csv` walks straight past it. Both files are written atomically, so
a reader never catches one half-written. Upgrading from a version that only had
`report.csv` carries its history over on the first run.

### One file per period

`REPORT_SPLIT = monthly` (or `daily`) publishes `report-2026-09.csv`,
`report-2026-08.csv`, … instead of one `report.csv`:

```
REPORT_DIR/
  report.state          the authority, as always
  report-2026-07.csv    closed: never rewritten again
  report-2026-08.csv    closed
  report-2026-09.csv    the only one this month's runs touch
```

This **partitions**, it does not snapshot: a row lives in exactly one file,
chosen by its `first_seen`, so reading the whole folder gives the report with
no duplicates to reconcile — which is precisely the shape Power BI's folder
connector expects, with no dedup step to write.

The practical gain is that a past period is never rewritten, so a spreadsheet
left open on last month's file cannot collide with this month's writes. A
closed file reopens only when one of its own rows moves on — a file first seen
in August that finally succeeds in September updates the August row where it
lives.

Size follows: at a thousand files a month, a monthly part is about a hundred
kilobytes and there are twelve a year. `daily` suits a much higher volume;
`none` stays right when a single file is easier to hand around. A period whose
rows have all aged past `REPORT_KEEP_DAYS` has its file removed.

## Logs

`LOG_DIR` holds two files:

Both are rotated by size, so a cron job cannot fill the disk:

```
dispatch.log      the current one, always
dispatch.log.1    the previous generation
...
dispatch.log.5    the oldest kept; the next rotation drops it
```

A log is rolled over when the line about to be written would take it past
`LOG_MAX_MB` (10 MB by default), keeping `LOG_KEEP` generations (5). Plain
renames, no compression, so `grep` still works across the set and the newest
lines are always in the unsuffixed file. Worst case on disk is
`(LOG_KEEP + 1) x LOG_MAX_MB` per log, so 60 MB each, 120 MB in total by
default. Failures are the bulky case — a `FAILURE` plus its `DIAG` line is
~700 bytes — which is exactly when you do not want the disk filling up.

Set `LOG_MAX_MB = 0` if `logrotate` already owns these files; running both
would have them fighting over the same names.

- **`dispatch.log`** — everything, at every level.
- **`errors.log`** — real failures only, i.e. the `ERROR` lines: something did
  not happen and needs a look. Watch this one. Warnings stay out of it: a file
  no rule claimed (`no rule matched`), a base name matching several data files,
  a setting written twice — observations about a run that otherwise went fine.
  Both levels still print to stderr, so nothing is hidden when running by hand.

Each outcome is one structured line, `<STATUS> <action>` followed by
`key='value'` fields:

```
[INFO]  SUCCESS move source='…/orders-42.csv' dest='/data/out/B/orders' target='…/orders-42.csv' (rule #12: …) archived='…/orders-42.json'
[ERROR] FAILURE move source='…/bad.xml' dest='…' reason='move failed' cause='[Errno 13] Permission denied' (rule #12: …) - left in place
[ERROR] FAILURE archive source='…/x.csv' target='…' reason='data moved but JSON archiving failed' cause='[Errno 28] No space left on device'
```

When a failure comes from the system or from a malformed sidecar, `cause=`
carries its own words — the errno and message for an I/O failure, the position
of the syntax error for a bad JSON — so the log says what to fix rather than
only that something did not work:

```
reason='move failed'                cause='[Errno 13] Permission denied'
reason='cannot create destination'  cause='[Errno 30] Read-only file system'
reason='invalid JSON'               cause='Expecting property name enclosed in double quotes: line 1 column 11 (char 10)'
reason='invalid JSON'               cause='top level is list, expected an object'
```

### How a file is moved

Never with a single opaque call. The move is a sequence of steps, each of which
can fail on its own and is named in the log as `step='...'`:

| Step | What it does |
|------|--------------|
| `check` | refuses up front when the outcome is already known: source not a readable regular file, destination missing or not writable, target name taken, not enough free space |
| *(rename)* | one atomic call. Nothing intermediate is visible and no data moves. Only within one filesystem — and some network mounts refuse it even there, in which case the staged path below takes over |
| `copy` | copies to a temporary name **in the destination directory**, then `fsync`s it — so a partial file never exists under the final name |
| `verify` | the copy's size matches the source's |
| `publish` | renames the temporary into place. A rename within one directory, so the file appears complete or not at all |
| `remove_source` | deletes the source, last. Until this call both copies exist, so an interrupted run loses nothing |

A failure at any step removes the temporary file and leaves the source
untouched, to be retried on the next run.

`remove_source` is the exception, and is reported as such: the file **has** been
delivered and only the source survives, so the next run would dispatch it a
second time. That line says so in as many words —
`reason='copied but the source could not be removed, it will be dispatched again next run'`.

**A failed move, or a destination that could not be created, is followed by a
`DIAG` line** carrying everything you would otherwise go and collect by hand —
because an errno alone does not say *permission on what, to do what*:

```
[ERROR] FAILURE move source='…/a.xml' dest='/data/out/locked' reason='move failed' cause='[Errno 13] Permission denied' (rule #7: …) - left in place
[ERROR] DIAG move failed_on='/data/out/locked/a.xml'
        source='…/a.xml'     mode=0664 owner=svc:svc  ours=rw
        source_dir='/data/incoming' mode=0775 owner=svc:svc  ours=rwx
        dest_dir='/data/out/locked' mode=0555 owner=root:root ours=rx
        same_filesystem=yes
        process=svc uid=1001 euid=1001 gid=1001 groups=svc,users umask=0002
        notes='writing into dest_dir needs write+execute on it, which we lack'
```

| Field | Answers |
|-------|---------|
| `failed_on` | the exact path the kernel refused (`filename2` too, for a rename) |
| `mode` / `owner` / `ours` | the permissions of each path, and what *this* process can do with it (`rwx`, or `none`) |
| `sticky=yes` | the directory only lets you remove your own files, whatever its mode says |
| `same_filesystem` | `no` means the move is a copy **then a delete**, so it also needs write+execute on `source_dir` |
| `process` | the account actually running: name, uid/euid, gid, groups, umask |
| `notes` | the mismatch found, in words — including that `EPERM` usually means the filesystem itself refuses (share mapping, read-only export, immutable attribute) rather than the mode bits |

Clean runs emit no `DIAG` lines.

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
