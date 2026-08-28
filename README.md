# file-dispatch

A small, dependency-light **file dispatcher**. On each run it scans an incoming
directory for data files that arrive together with a JSON *sidecar* holding their
metadata, then — driven by rules in a single config file — **moves** each data
file to a destination directory, **archives** the JSON, and **logs** every action.

It is designed to be run periodically from **cron**, and to be configured by
someone who is not a programmer: everything lives in one plain config file.

- One data file + its `.json` sidecar sharing the same base name form a *pair*
  (e.g. `orders-42.csv` + `orders-42.json`).
- Routing rules read the JSON fields and pick a destination.
- If no rule matches, the file is **left untouched** and the reason is logged.
- Nothing is ever deleted: a file ends up routed, or it stays in place.

## Requirements

Just two things, both present on virtually every Linux box out of the box:

- a POSIX shell at `/bin/sh` (only used by the tiny launcher)
- `python3` >= 3.6 (**standard library only** — nothing to `pip install`)

That's it — no `jq`, no `flock`, no coreutils commands: the program does the
locking (`fcntl`), file moves (`shutil`), timing, JSON parsing, etc. all in
Python. `lsof` is **optional**: if installed it is used as an extra "file is open
for writing" check, but everything works fine without it.

If Python isn't already there:

| Distro           | Install command             |
|------------------|-----------------------------|
| Debian / Ubuntu  | `sudo apt install python3`   |
| RHEL / Fedora    | `sudo dnf install python3`   |
| openSUSE         | `sudo zypper install python3` |
| Arch             | `sudo pacman -S python`      |
| Alpine           | `sudo apk add python3`       |

You can pin which Python to use with the `PYTHON` setting (see below) or the
`DISPATCH_PYTHON` environment variable; otherwise `python3` from `PATH` is used.

## Architecture

Three files, a clean split — almost everything is Python:

- **`dispatch.sh`** — a ~15-line POSIX-shell **launcher**. Its only job is to pick
  the Python interpreter and hand off to `dispatch.py`.
- **`dispatch.py`** — the **program** (Python, stdlib only): CLI, config
  resolution, the cron lock, scanning the incoming directory, pairing files, the
  I/O-stability check, moving files, and logging.
- **`engine.py`** — the **engine** (Python, stdlib only, unit-tested): parsing the
  config DSL, the rule grammar (`AND`/`OR`/`IN`/wildcards/quotes/concatenation),
  variable expansion, and matching a JSON file against the rules.

The pipeline each run: **parse → preflight → pair → stabilize → resolve → move**.
Run it via `./dispatch.sh …` (or directly with `python3 dispatch.py …`).

## Setup

```sh
git clone <your-repo-url> file-dispatch
cd file-dispatch
cp dispatch.conf.example dispatch.conf
$EDITOR dispatch.conf          # set your directories and rules
./dispatch.sh --check          # validate the config without processing anything
```

Then try it out without touching any file, then run it for real:

```sh
./dispatch.sh --dry-run        # log what WOULD happen, move nothing
./dispatch.sh --debug          # verbose trace (see "Debugging" below)
./dispatch.sh                  # process for real
```

In `--dry-run` mode nothing is moved or archived; every log line is prefixed with
`DRY-RUN` so it is obvious it was a preview.

### Where the config file comes from

The config path is resolved in this order (first match wins):

1. `--config-file /path/to/dispatch.conf`
2. the `DISPATCH_CONFIG` environment variable
3. `dispatch.conf` next to the script (the default)

```sh
./dispatch.sh --config-file /etc/file-dispatch/prod.conf
DISPATCH_CONFIG=/etc/file-dispatch/prod.conf ./dispatch.sh
```

## Running from cron

Process the incoming directory every 2 minutes:

```cron
*/2 * * * * /path/to/file-dispatch/dispatch.sh --config-file /path/to/file-dispatch/dispatch.conf >> /path/to/file-dispatch/logs/cron.log 2>&1
```

Overlapping runs are prevented automatically with a lock, so a slow run is never
started twice.

Rotate the log with `logrotate` (`/etc/logrotate.d/file-dispatch`):

```
/path/to/file-dispatch/logs/*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
}
```

## The config file

One file, three parts: **Settings**, optional **Validation**, **Variables**, and
**Rules**. Lines starting with `#` are comments. See
[`dispatch.conf.example`](dispatch.conf.example) for the fully commented template.

### Settings

```ini
INCOMING_DIR     = "/data/incoming"      # directory to watch
JSON_ARCHIVE_DIR = "/data/archive/json"  # where every processed .json goes
LOG_DIR          = "/data/logs"          # logs folder (see below)
STABLE_SECONDS   = 2                     # optional: I/O-settle delay (default 2)
# PYTHON         = "/usr/bin/python3"    # optional: interpreter for the engine
```

`LOG_DIR` is a **folder** that holds two files:

- `dispatch.log` — every action (INFO, WARN, ERROR)
- `errors.log` — the warnings and errors only, so problems are easy to spot

### One rule to remember: `$` means "the value of"

Exactly like a shell:

- `NAME = ...` **defines** a value.
- `$NAME` **uses** a value — a JSON field or one of your own variables.

Every field of the current JSON is available as `$fieldname`.

### Validation (optional)

Require some JSON fields to be present **and non-empty** before anything happens.
If any is missing or empty, the file is left in place and an error is logged.

```ini
REQUIRED = $category, $group
```

### Variables

Build destination paths from JSON fields and from other variables (evaluated top
to bottom). Adjacent pieces are concatenated, exactly like in a shell:

```ini
OUT     = "/data/out"
GROUP   = "$group"
TARGET  = $group"/"$category        # -> "<group>/<category>"
REPORTS = "$OUT/Annual Reports"     # quotes let a value contain spaces
```

### Rules

```
<condition> => "<destination directory>"
```

- Condition atoms:
  - `$field = "value"` — equality
  - `$field IN ("a", "b", "c")` — membership
- Combine atoms with `AND` / `OR`. **`AND` is evaluated before `OR`.**
- `*` in a value is a wildcard: `"invoice*"`, `"*credit"`, `"*"` (= any value).
- Quote a value or path when it contains spaces or commas. Quoting a simple
  value is optional (`"report"` and `report` are the same); the examples quote
  everything for a consistent look.
- The **first** matching rule wins. If none matches, the file stays in place.

## Rule cookbook

Each example shows the incoming JSON and where the data file lands, assuming:

```ini
OUT   = "/data/out"
GROUP = "$group"
```

| Rule | JSON | Data file goes to |
|------|------|-------------------|
| `$category = "report" => "$OUT/$GROUP/reports"` | `{"category":"report","group":"B"}` | `/data/out/B/reports/` |
| `$category IN ("invoice","credit","debit") => "$OUT/billing"` | `{"category":"credit"}` | `/data/out/billing/` |
| `$category = "arch*" => "$OUT/archive"` | `{"category":"archived"}` | `/data/out/archive/` |
| `$type = "export" AND $status IN ("new","retry") => "$OUT/exports"` | `{"type":"export","status":"retry"}` | `/data/out/exports/` |
| `$priority = "high" OR $flag = "urgent" => "$OUT/urgent"` | `{"flag":"urgent"}` | `/data/out/urgent/` |
| `$status IN ("in progress","on hold") => "$OUT/pending files"` | `{"status":"on hold"}` | `/data/out/pending files/` |
| `$kind = "*" => "$OUT/$GROUP/$kind"` | `{"kind":"memo","group":"B"}` | `/data/out/B/memo/` |
| *(no rule matches)* | `{"category":"unknown"}` | *stays in the incoming directory (logged)* |

### How to add a rule for a new kind of file

1. Look in the log for the `no rule matched ... (fields: ...)` line — it shows the
   JSON fields of the file that was not routed.
2. Add one line to the Rules section of `dispatch.conf`, e.g.
   `$category = "newthing" => "$OUT/newthing"`.
3. Run `./dispatch.sh --check` to make sure the config is still valid.

No need to touch the script.

## What happens on each run

1. **Validate the config** first. On any error, all problems are logged (with line
   numbers) and the run stops without touching a single file. (`--check` does only
   this step.)
2. Take a lock so two cron runs never overlap.
3. Pair each `.json` with its data file; incomplete pairs wait for the next run.
4. Skip any pair whose files are still changing (still being copied).
5. For each complete, stable pair: validate JSON, check `REQUIRED`, evaluate the
   rules, then move the data file and archive the JSON.

### Log format

Every outcome is one line. A completed move carries a **`SUCCESS`** status, the
**source** path, the **exact destination directory**, the final path, the rule
that matched, and where the JSON was archived:

```
2026-08-28T09:15:03+0000 [INFO] SUCCESS source='/data/incoming/orders-42.csv' dest='/data/out/B/orders' target='/data/out/B/orders/orders-42.csv' (rule #12: $category = "order" => "$OUT/$GROUP/orders") archived='/data/archive/json/orders-42.json'
```

Anything that goes wrong carries a **`FAILURE`** status, the source, and the
reason (and the file stays in place):

```
2026-08-28T09:15:04+0000 [ERROR] FAILURE source='/data/incoming/bad.xml' dest='/data/out/x' reason='move failed' - left in place
```

Both files live under `LOG_DIR`: `dispatch.log` (everything) and `errors.log`
(the `WARN`/`FAILURE` lines only).

### Debugging

`--debug` (or `-d`) adds a verbose trace to `dispatch.log` (and stderr): the value
of every JSON field, every variable as it is resolved, and the rule resolution
step by step — each condition atom, which rule matched, and the resolved
destination.

```
[DEBUG]   json field: $category = 'report'
[DEBUG]   variable: GROUP = 'B'
[DEBUG]   rule #8: $category = "report" => "$OUT/$GROUP/reports"
[DEBUG]       atom $category = "report"  ('report')  -> true
[DEBUG]   -> MATCH; destination resolves to '/data/out/B/reports'
```

## Security notes

Incoming files come from other systems and are treated as untrusted; the config is
trusted (written by the operator). The script:

- **never** `eval`s or sources the config or the JSON — a field value such as
  `$(cmd)` is only ever data, never executed;
- rejects destinations that try to escape via `..`;
- quotes all filesystem arguments and uses `--`, so odd filenames (spaces,
  leading dashes) are harmless;
- refuses symlinked data files;
- strips control characters from log messages to prevent log/terminal injection.

## Tests

```sh
./tests/run_tests.sh
```

Two layers, no external framework:

- **`tests/test_engine.py`** — Python unit tests for the engine's pure functions
  (tokenizer, `assemble_value`, `parse_atom`, and resolution scenarios).
- **`tests/run_tests.sh`** — end-to-end tests that build an isolated sandbox per
  case and check the full functional scope: routing, variables, operators
  (`=`, `IN`, `AND`, `OR`, `*`), quoting and concatenation, validation, incomplete
  pairs, I/O stability, idempotence, config preflight, `--dry-run`/`--debug`,
  the `PYTHON`/config resolution, and the security guards.

`run_tests.sh` runs the unit tests first, then the end-to-end cases.

## License

[MIT](LICENSE)
