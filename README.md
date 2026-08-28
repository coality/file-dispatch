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

- `bash` >= 4
- `jq`
- `flock` (from util-linux)

All three are available in every mainstream distribution's package repository:

| Distro           | Install command              |
|------------------|------------------------------|
| Debian / Ubuntu  | `sudo apt install jq util-linux` |
| RHEL / Fedora    | `sudo dnf install jq util-linux` |
| openSUSE         | `sudo zypper install jq util-linux` |
| Arch             | `sudo pacman -S jq util-linux` |
| Alpine           | `sudo apk add bash jq util-linux` |

`mv`, `mkdir`, `stat`, `date`, `sleep` (all coreutils) are used too and are always
present. `lsof` is **optional**: if it happens to be installed it is used as an
extra "file is open for writing" check, but the script works fine without it.
No other command is required — no `awk`, `sed`, `find`, `basename`, `dirname`.

## Setup

```sh
git clone <your-repo-url> file-dispatch
cd file-dispatch
cp dispatch.conf.example dispatch.conf
$EDITOR dispatch.conf          # set your directories and rules
./dispatch.sh --check          # validate the config without processing anything
```

Then run it once by hand to try it out:

```sh
./dispatch.sh
```

## Running from cron

Process the incoming directory every 2 minutes:

```cron
*/2 * * * * /path/to/file-dispatch/dispatch.sh /path/to/file-dispatch/dispatch.conf >> /path/to/file-dispatch/logs/cron.log 2>&1
```

Overlapping runs are prevented automatically with a lock, so a slow run is never
started twice.

Rotate the log with `logrotate` (`/etc/logrotate.d/file-dispatch`):

```
/path/to/file-dispatch/logs/dispatch.log {
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
INCOMING_DIR     = "/data/incoming"          # directory to watch
JSON_ARCHIVE_DIR = "/data/archive/json"      # where every processed .json goes
LOG_FILE         = "/data/logs/dispatch.log" # action log
STABLE_SECONDS   = 2                          # optional: I/O-settle delay (default 2)
```

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
   rules, then move the data file and archive the JSON. Every move is logged with
   the destination and the rule that matched:

   ```
   2026-08-28T09:15:03+0000 [INFO] moved 'orders-42.csv' -> '/data/out/B/orders' (rule #12: $category = "order" => "$OUT/$GROUP/orders")
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

A self-contained suite (no external framework) that builds an isolated sandbox per
case and checks the full functional scope: routing, variables, operators
(`=`, `IN`, `AND`, `OR`, `*`), quoting and concatenation, validation, incomplete
pairs, I/O stability, idempotence, config preflight, and the security guards.

## License

[MIT](LICENSE)
