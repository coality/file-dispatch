#!/usr/bin/env python3
"""
file-dispatch - route incoming data files to destination directories based on
the metadata carried by their JSON sidecar, driven by a single config file.

This is the whole program: CLI, config resolution, the cron lock, scanning the
incoming directory, pairing files, the I/O-stability check, moving files and
logging. The parsing / matching core lives in engine.py (imported below).

Normally launched through dispatch.sh (a tiny shell launcher that just picks the
Python interpreter), but it can also be run directly: `python3 dispatch.py ...`.

What one run does, in order:

  1. read + validate the config          -> stop here on --check, or on errors
  2. take an exclusive lock on LOG_DIR   -> a slow run never overlaps the next
                                            cron tick (skipped by --dry-run)
  3. pair up INCOMING_DIR                -> <base>.json + exactly one sibling
  4. wait for the pairs to settle        -> size/mtime unchanged, nobody
                                            writing (see paths_open_for_write)
  5. per pair: resolve, move, archive    -> engine decides, this file acts

Every step that gives up on a file leaves it exactly where it was and logs why,
so the next run picks it up again. Nothing is ever deleted, and a file is only
ever moved after its destination has been confirmed usable -- which is what
lets a cron job run unattended.

Standard library only. Requires Python 3.9 or newer.
"""

import argparse
import csv
import fcntl
import grp
import os
import pwd
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime

import engine

MIN_PYTHON = (3, 9)

# Runtime state (filled from the config / CLI)
INCOMING_DIR = ""
JSON_ARCHIVE_DIR = ""
LOG_DIR = ""
STABLE_SECONDS = 2
CREATE_DIRS = False
DISPATCH_WITHOUT_JSON = False
LOG_FILE = ""
ERROR_LOG = ""
LOG_MAX_BYTES = 10 * 1024 * 1024        # 0 disables rotation
LOG_KEEP = 5
REPORT_DIR = ""                         # "" disables the report entirely
REPORT_KEEP_DAYS = 90
REPORT_SPLIT = "none"                   # none | daily | monthly

DRY_RUN = False
DEBUG = False

CFG = None  # engine.Config

# Counters
PROCESSED = UNMATCHED = INVALID = INCOMPLETE = UNSTABLE = ERRORS = 0

_CTRL = {c: None for c in (0, 7, 8, 11, 12, 27)}  # NUL BEL BS VT FF ESC


# --------------------------------------------------------------------------- #
# Logging (dispatch.log = everything, errors.log = WARN/ERROR)
# --------------------------------------------------------------------------- #
def sanitize(s):
    s = s.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return s.translate(_CTRL)


def _rotate(path, incoming):
    """Roll 'path' over before writing 'incoming' bytes would exceed the cap.

    dispatch.log -> dispatch.log.1 -> ... -> dispatch.log.<LOG_KEEP>, oldest
    dropped. Plain renames, so the files stay greppable and the newest is
    always the unsuffixed one. LOG_MAX_BYTES = 0 turns this off, for hosts
    where logrotate already owns these files.

    Rotation happens under the run lock, so two real runs cannot interleave
    here. A --dry-run takes no lock, so it could in principle rotate at the
    same instant as a real run; the worst outcome is a few lines landing in
    the file that has just been rolled over.
    """
    if LOG_MAX_BYTES <= 0:
        return
    try:
        if os.path.getsize(path) + incoming <= LOG_MAX_BYTES:
            return
    except OSError:
        return                              # not there yet: nothing to rotate
    if LOG_KEEP <= 0:
        try:
            os.remove(path)                 # keep nothing: start over
        except OSError:
            pass
        return
    try:
        os.remove("%s.%d" % (path, LOG_KEEP))
    except OSError:
        pass
    for i in range(LOG_KEEP - 1, 0, -1):
        try:
            os.replace("%s.%d" % (path, i), "%s.%d" % (path, i + 1))
        except OSError:
            pass
    try:
        os.replace(path, path + ".1")
    except OSError:
        pass


def _append(path, line):
    try:
        _rotate(path, len(line.encode("utf-8")) + 1)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def log(level, msg):
    msg = sanitize(msg)
    if DRY_RUN:
        msg = "DRY-RUN " + msg
    ts = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    line = "%s [%s] %s" % (ts, level, msg)
    if LOG_FILE:
        _append(LOG_FILE, line)
    # errors.log is the file you watch, so only real failures go in it: things
    # that did not happen and need someone to look. A WARN is an observation
    # about a run that otherwise went fine -- a file no rule claimed, a setting
    # written twice -- and stays in dispatch.log. Both still reach stderr, so
    # nothing becomes invisible when running by hand.
    if level == "ERROR" and ERROR_LOG:
        _append(ERROR_LOG, line)
    if level in ("ERROR", "WARN"):
        print(line, file=sys.stderr)
    elif level == "DEBUG":
        print(line, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Filesystem helpers
# --------------------------------------------------------------------------- #
_DEST_CHECK_CACHE = {}


def dest_problem(path):
    """Why writing into 'path' would fail, or None if it looks usable.

    Answers the question a real run only asks when it is too late: could we
    create this destination, and could we write into it? Used by --dry-run so
    that a broken or read-only target shows up before cron hits it. Cached:
    many files usually share one destination.
    """
    if path not in _DEST_CHECK_CACHE:
        _DEST_CHECK_CACHE[path] = _dest_problem(path)
    return _DEST_CHECK_CACHE[path]


def _dest_problem(path):
    if not path:
        return "empty destination"
    if os.path.isdir(path):
        if not os.access(path, os.W_OK | os.X_OK):
            return "destination directory is not writable"
        return None
    if os.path.exists(path):
        return "destination exists but is not a directory"
    if not CREATE_DIRS:
        return "destination directory does not exist (CREATE_DIRS is no)"
    # Not there yet: a real run would create it. Walk up to the closest
    # existing ancestor and check the missing levels could be created there.
    parent = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.isdir(parent):
            if not os.access(parent, os.W_OK | os.X_OK):
                return "cannot create destination: '%s' is not writable" % parent
            return None
        if os.path.exists(parent):
            return "cannot create destination: '%s' is not a directory" % parent
        up = os.path.dirname(parent)
        if up == parent:
            return None
        parent = up


def collision_safe(directory, name):
    """A path under 'directory' for 'name' that does not overwrite anything.

    Two files with the same name arriving on different days is normal, so a
    collision suffixes the timestamp (and then the pid, if a run is fast enough
    to collide within one second) instead of replacing what is already there.
    """
    path = os.path.join(directory, name)
    if os.path.exists(path):
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(directory, "%s.%s" % (name, ts))
        if os.path.exists(path):
            path = os.path.join(directory, "%s.%s.%d" % (name, ts, os.getpid()))
    return path


def paths_open_for_write(paths):
    """Subset of 'paths' that some local process currently holds open for writing.

    Catches the producer that is still copying a file in but happens to be idle
    during the stability window, which the size/mtime comparison cannot see.
    Answered for every path in one pass: on Linux by walking /proc (a few
    milliseconds), elsewhere by a single lsof call. Best effort -- a writer
    running as another user, or one on the far side of a network share, is
    invisible either way, so this only ever adds safety.
    """
    if not paths:
        return set()
    if os.path.isdir("/proc/self/fd"):
        return _writers_from_proc(paths)
    return _writers_from_lsof(paths)


def _writers_from_proc(paths):
    want, busy = {}, set()
    for p in paths:
        want.setdefault(os.path.realpath(p), p)
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fddir = "/proc/%s/fd" % pid
        try:
            fds = os.listdir(fddir)
        except OSError:
            continue                    # process gone, or not ours to inspect
        for fd in fds:
            try:
                target = os.readlink("%s/%s" % (fddir, fd))
            except OSError:
                continue
            path = want.get(target)
            if path is None or path in busy:
                continue
            try:
                with open("/proc/%s/fdinfo/%s" % (pid, fd)) as fh:
                    for line in fh:
                        if line.startswith("flags:"):
                            if int(line.split()[1], 8) & os.O_ACCMODE:
                                busy.add(path)   # O_WRONLY or O_RDWR
                            break
            except (OSError, ValueError, IndexError):
                continue
    return busy


def _writers_from_lsof(paths):
    lsof = shutil.which("lsof")
    if not lsof:
        return set()
    want, busy, mode = {}, set(), ""
    for p in paths:
        want.setdefault(os.path.realpath(p), p)
    try:
        out = subprocess.run([lsof, "-F", "an", "--", *paths],
                             capture_output=True, text=True).stdout
    except OSError:
        return set()
    for line in out.splitlines():
        if line.startswith("f"):
            mode = ""
        elif line.startswith("a"):
            mode = line[1:]
        elif line.startswith("n") and mode in ("u", "w"):
            path = want.get(os.path.realpath(line[1:]))
            if path is not None:
                busy.add(path)
    return busy


def system_fields(path):
    """What the filesystem knows about a data file, as rule fields.

    Always available, sidecar or not (engine.SYSTEM_FIELDS documents them).
    Filesize is digits so the numeric operators work on it; Filedatetime is
    "YYYY-MM-DDTHH:MM:SS" in local time, which STARTSWITH and wildcards can
    slice by year, month or day. A file we cannot stat yields empty strings
    rather than an error: no rule will match them, and the file stays put.
    """
    try:
        st = os.stat(path)
        size = str(st.st_size)
        stamp = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        size, stamp = "", ""
    return {"Filename": os.path.basename(path), "Filesize": size, "Filedatetime": stamp}


def why(exc):
    """A short, log-safe cause for an exception, for the cause='...' log field.

    An OSError already carries the two things worth reporting -- the errno and
    the system's own wording -- and the paths are named elsewhere in the line,
    so they are dropped here. Quotes are stripped so cause='...' stays one
    readable field.
    """
    errno_, strerror = getattr(exc, "errno", None), getattr(exc, "strerror", None)
    if errno_ is not None and strerror:
        return "[Errno %d] %s" % (errno_, strerror)
    return sanitize(str(exc)).replace("'", "").strip() or exc.__class__.__name__


# Read once: os.umask has to set a value to read one, and doing that inside an
# error path would briefly change the mask for anything running concurrently.
try:
    _UMASK = os.umask(0)
    os.umask(_UMASK)
except OSError:                                     # pragma: no cover
    _UMASK = None


def _owner(st):
    """'user:group' for a stat result, falling back to numeric ids."""
    try:
        user = pwd.getpwuid(st.st_uid).pw_name
    except (KeyError, OSError):
        user = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except (KeyError, OSError):
        group = str(st.st_gid)
    return "%s:%s" % (user, group)


def _path_facts(label, path):
    """What this process can see and do about one path."""
    if not path:
        return "%s='' (not set)" % label
    try:
        st = os.stat(path)
    except OSError as exc:
        return "%s='%s' (%s)" % (label, path, why(exc))
    ours = "".join(c for c, ok in zip("rwx", (os.access(path, os.R_OK),
                                              os.access(path, os.W_OK),
                                              os.access(path, os.X_OK))) if ok) or "none"
    out = "%s='%s' mode=%04o owner=%s ours=%s" % (
        label, path, stat.S_IMODE(st.st_mode), _owner(st), ours)
    if st.st_mode & stat.S_ISVTX:
        # /tmp-style: you may only remove your own files, whatever the mode says
        out += " sticky=yes"
    return out


def whoami():
    """The identity the process is actually running under."""
    try:
        name = pwd.getpwuid(os.geteuid()).pw_name
    except (KeyError, OSError):
        name = "?"
    try:
        groups = ",".join(sorted(_group_name(g) for g in os.getgroups()))
    except OSError:                                 # pragma: no cover
        groups = "?"
    out = "process=%s uid=%d euid=%d gid=%d groups=%s" % (
        name, os.getuid(), os.geteuid(), os.getgid(), groups)
    if _UMASK is not None:
        out += " umask=%04o" % _UMASK
    return out


def _group_name(gid):
    try:
        return grp.getgrgid(gid).gr_name
    except (KeyError, OSError):
        return str(gid)


def _same_filesystem(a, b):
    """'yes'/'no'/'?' -- a cross-device move copies and deletes instead of
    renaming, which needs different permissions and can fail differently."""
    try:
        return "yes" if os.stat(a).st_dev == os.stat(b).st_dev else "no"
    except OSError:
        return "?"


def _nearest_existing(path):
    """The closest existing ancestor of 'path' -- the directory whose
    permissions actually decide whether the missing levels can be created."""
    p = os.path.abspath(path or ".")
    while not os.path.isdir(p):
        up = os.path.dirname(p)
        if up == p:
            return p
        p = up
    return p


def diagnose(src, dest_dir, exc=None):
    """Everything that decides whether moving 'src' into 'dest_dir' can work.

    A bare errno says almost nothing on its own -- "Operation not permitted"
    is the same message whether the rename was refused, the source could not
    be removed, or the share forbids it. These are the facts you would go and
    collect by hand: who we are, what the three paths involved look like, and
    whether the move is a rename or a copy-and-delete.
    """
    src_dir = os.path.dirname(os.path.abspath(src))
    parts = []
    # The kernel says which path it refused, which answers "permission on what?"
    # before any of the rest has to be read.
    for attr, label in (("filename", "failed_on"), ("filename2", "failed_on2")):
        target = getattr(exc, attr, None)
        if target:
            parts.append("%s='%s'" % (label, target))
    parts += [_path_facts("source", src),
              _path_facts("source_dir", src_dir),
              _path_facts("dest_dir", dest_dir),
              "same_filesystem=%s" % _same_filesystem(src_dir, dest_dir),
              whoami()]
    notes = []
    if _same_filesystem(src_dir, dest_dir) == "no":
        notes.append("a cross-filesystem move copies the file then deletes the "
                     "source, so it needs write on dest_dir AND write+execute on source_dir")
    if not os.access(src_dir, os.W_OK | os.X_OK):
        notes.append("removing the source needs write+execute on source_dir, which we lack")
    if dest_dir and os.path.isdir(dest_dir) and not os.access(dest_dir, os.W_OK | os.X_OK):
        notes.append("writing into dest_dir needs write+execute on it, which we lack")
    if getattr(exc, "errno", None) == 1:            # EPERM
        notes.append("EPERM rather than EACCES often means the filesystem itself refuses "
                     "(network share mapping, read-only export, immutable attribute), "
                     "not the mode bits")
    if notes:
        parts.append("notes='%s'" % "; ".join(notes))
    return " ".join(parts)


def move_file(src, target):
    """Move 'src' to 'target' as a series of checked, recoverable steps.

    Returns (step, exc): (None, None) on success, otherwise the step that
    failed and the exception behind it, so the caller can say what went wrong
    rather than just that something did.

    Two paths, in order of preference:

      rename   one atomic call. Nothing intermediate is ever visible and no
               data moves. Only possible within one filesystem, and some
               network mounts refuse it even there.

      staged   copy to a temporary name in the DESTINATION directory, flush it
               to disk, check its size, then rename it into place -- a rename
               within one directory, so the file appears complete or not at
               all, never half-written under its final name. The source is
               removed last: until that call, both copies exist and nothing is
               lost if the run dies.

    The staged path is also the fallback when rename is refused, which is what
    makes a share that forbids renaming but allows create+write+delete work.
    """
    checks = _premove_checks(src, target)
    if checks:
        return checks

    try:                                        # fast path: atomic, no copying
        os.rename(src, target)
        return (None, None)
    except OSError as rename_exc:
        pass                                    # fall through to the staged path

    tmp = os.path.join(os.path.dirname(target),
                       ".%s.partial-%d" % (os.path.basename(target), os.getpid()))
    try:
        with open(src, "rb") as fsrc, open(tmp, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst)
            fdst.flush()
            os.fsync(fdst.fileno())             # on disk, not just in the cache
    except OSError as exc:
        _discard(tmp)
        return ("copy", exc)

    try:
        shutil.copystat(src, tmp)               # keep mtime/mode; not fatal
    except OSError:
        pass

    try:
        if os.path.getsize(tmp) != os.path.getsize(src):
            _discard(tmp)
            return ("verify", OSError("copied size differs from the source"))
    except OSError as exc:
        _discard(tmp)
        return ("verify", exc)

    try:
        os.replace(tmp, target)                 # atomic within the directory
    except OSError as exc:
        _discard(tmp)
        return ("publish", exc)

    try:
        os.remove(src)
    except OSError as exc:
        # The file IS delivered; only the source survives. Reported separately
        # by the caller, because the next run would dispatch it a second time.
        return ("remove_source", exc)
    return (None, None)


def _premove_checks(src, target):
    """Refuse before touching anything, when we can already tell it will fail."""
    dest_dir = os.path.dirname(target)
    if not os.path.isfile(src):
        return ("check", OSError("source is not a regular file"))
    if not os.access(src, os.R_OK):
        return ("check", PermissionError(13, "source is not readable", src))
    if not os.path.isdir(dest_dir):
        return ("check", OSError(2, "destination directory does not exist", dest_dir))
    if not os.access(dest_dir, os.W_OK | os.X_OK):
        return ("check", PermissionError(13, "destination directory is not writable", dest_dir))
    if os.path.exists(target):
        return ("check", OSError(17, "target already exists", target))
    try:
        need = os.path.getsize(src)
        free = shutil.disk_usage(dest_dir).free
        if free < need:
            return ("check", OSError(28, "not enough space: needs %d bytes, %d free"
                                     % (need, free), dest_dir))
    except OSError:
        pass                                    # cannot tell; let the copy decide
    return None


def _discard(path):
    """Remove a partial copy; never mask the failure that led here."""
    try:
        os.remove(path)
    except OSError:
        pass


def _sig(path):
    try:
        st = os.stat(path)
        return (st.st_size, int(st.st_mtime))
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# The CSV report
#
# One row per FILE -- not per name and not per run. The logs say what happened
# during one run; this says where a given file stands, which is the question
# asked after the fact ("what became of orders-42.csv?").
#
# A row is identified by (filename, first_seen), so a name that comes back
# later is a new file with its own history rather than an addition to the old
# one -- periodic exports reuse names constantly. A row that reaches success is
# closed: a later file of the same name opens a fresh row.
#
# While a file keeps failing, its row is updated in place and 'retries' counts
# the runs that tried again, so one stuck file is one line, not one per run.
# --------------------------------------------------------------------------- #
REPORT_COLUMNS = ("filename", "first_seen", "file_date", "destination",
                  "moved_at", "status", "retries", "reason")

_report = None          # {(filename, first_seen): row}; None until loaded
_report_seen = set()    # keys touched this run, so retention can spare them


def _now(precise=False):
    """A timestamp for the report. 'precise' adds milliseconds, which only
    first_seen needs: it is half of a row's identity, and two files of the same
    name can turn up within the same second."""
    now = datetime.now().astimezone()
    if precise:
        return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    return now.strftime("%Y-%m-%dT%H:%M:%S")


def _file_date(path):
    try:
        return datetime.fromtimestamp(os.stat(path).st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def report_path():
    """The published copy: what Excel and Power BI open."""
    return os.path.join(REPORT_DIR, "report.csv") if REPORT_DIR else ""


def _period(row):
    """Which published file a row belongs to, from its first_seen.

    Partitioning, not snapshotting: a row lives in exactly one file, so the
    whole set read together is the report -- no duplicates to reconcile, which
    is what makes a Power BI folder import work with no extra step.
    """
    if REPORT_SPLIT == "monthly":
        return (row.get("first_seen") or "")[:7]        # YYYY-MM
    if REPORT_SPLIT == "daily":
        return (row.get("first_seen") or "")[:10]       # YYYY-MM-DD
    return ""


def _published_path(period):
    name = "report-%s.csv" % period if period else "report.csv"
    return os.path.join(REPORT_DIR, name)


def report_state_path():
    """Our own copy, and the authority.

    Kept apart from report.csv because a spreadsheet left open on a network
    share holds an SMB deny-write lock, and the rename that publishes the
    report is then refused -- so publishing is a thing that is allowed to fail.
    The state is what must not. No .csv extension, so a Power BI folder import
    filtering *.csv walks past it and nobody opens it by accident.
    """
    return os.path.join(REPORT_DIR, "report.state") if REPORT_DIR else ""


def report_load():
    """Read the state, keyed by (filename, first_seen).

    Falls back to the published report.csv when there is no state yet, so an
    existing report carries over the first time this runs.
    """
    global _report
    _report = {}
    if not REPORT_DIR:
        return
    path = report_state_path()
    if not os.path.exists(path):
        path = report_path()
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                key = (row.get("filename", ""), row.get("first_seen", ""))
                if key[0]:
                    _report[key] = {c: row.get(c, "") or "" for c in REPORT_COLUMNS}
    except (OSError, csv.Error):
        _report = {}        # unreadable or corrupt: start over rather than lose the run


def report_note(path, status, destination="", reason="", file_date=None):
    """Record where 'path' stands. Called once per file per run.

    Reuses the file's open row if it has one -- counting a retry -- and opens a
    new one otherwise. Success closes the row.
    """
    if _report is None or not REPORT_DIR:
        return
    name = os.path.basename(path)
    open_rows = [k for k, r in _report.items()
                 if k[0] == name and r.get("status") != "success"]
    if open_rows:
        key = max(open_rows, key=lambda k: k[1])        # the most recent one
        row = _report[key]
        if key not in _report_seen:
            row["retries"] = str(int(row.get("retries") or 0) + 1)
    else:
        key = (name, _now(precise=True))
        row = dict.fromkeys(REPORT_COLUMNS, "")
        row.update(filename=name, first_seen=key[1], retries="0")
        _report[key] = row

    _report_seen.add(key)
    row["status"] = status
    # On success the source is already gone, so the caller passes the date it
    # read before the move; everywhere else the file is still there to stat.
    row["file_date"] = (file_date if file_date is not None else _file_date(path)) or row["file_date"]
    if destination:
        row["destination"] = destination
    row["reason"] = reason
    if status == "success":
        row["moved_at"] = _now()
    return key


def report_save():
    """Write the state, then publish report.csv from it.

    Two steps on purpose. The state must be written -- losing it loses the
    retry counts and the first_seen of everything in flight -- while publishing
    is best effort: a spreadsheet holding report.csv open on a share makes the
    rename fail, and that must not cost us the run's bookkeeping. The published
    copy simply stays as it was and is rewritten on the next run.
    """
    if _report is None or not REPORT_DIR:
        return
    try:
        os.makedirs(REPORT_DIR, exist_ok=True)
    except OSError as exc:
        log("ERROR", "cannot create report directory '%s' cause='%s'" % (REPORT_DIR, why(exc)))
        return

    rows = [r for k, r in _report.items() if k in _report_seen or _report_keep(r)]
    rows.sort(key=lambda r: (r.get("first_seen", ""), r.get("filename", "")))

    if not _report_seen and len(rows) == len(_report) and not _publish_lagging(rows):
        # Nothing was observed, nothing aged out, and every published file is
        # already in step with the state. A quiet cron would otherwise rewrite
        # the whole report every minute, which on a network share is real
        # traffic for no change at all.
        return

    if not _write_csv(report_state_path(), rows):
        return                              # nothing to publish from

    # Group the rows into the files they belong to. With REPORT_SPLIT = none
    # that is the single report.csv, as before.
    by_period = {}
    for row in rows:
        by_period.setdefault(_period(row), []).append(row)

    touched = {_period(_report[k]) for k in _report_seen if k in _report}
    for period, part in sorted(by_period.items()):
        # Only rewrite a file this run actually changed. Past periods sit
        # untouched, so a spreadsheet open on last month's file never collides
        # with this month's writes -- and a period only reopens when one of
        # its rows moves on, such as an old failure that finally succeeds.
        if period and period not in touched and os.path.exists(_published_path(period)):
            continue
        if not _write_csv(_published_path(period), part):
            log("WARN", "%s could not be updated (a program is holding it open?) - the state "
                        "is safe in '%s' and it will be published on the next run"
                % (os.path.basename(_published_path(period)), report_state_path()))

    # A period emptied by retention leaves a file behind; drop it.
    if REPORT_SPLIT != "none":
        _prune_published(set(by_period))


def _prune_published(keep):
    try:
        names = os.listdir(REPORT_DIR)
    except OSError:
        return
    for name in names:
        if not (name.startswith("report-") and name.endswith(".csv")):
            continue
        if name[len("report-"):-len(".csv")] in keep:
            continue
        try:
            os.remove(os.path.join(REPORT_DIR, name))
        except OSError:
            pass


def _publish_lagging(rows):
    """True when a published file is missing, or older than the state.

    The quiet-run shortcut in report_save() assumes the files on disk already
    say what the state says. That is false after a publish that FAILED -- a
    spreadsheet holding report.csv open being the usual reason -- and without
    this check the report would stay stale until the next file happened to
    arrive. On a quiet weekend that is days, and it is the one case where the
    two-file design would otherwise not heal itself.
    """
    try:
        state_mtime = os.path.getmtime(report_state_path())
    except OSError:
        return True                          # no state yet: publish
    for period in {_period(r) for r in rows}:
        try:
            if os.path.getmtime(_published_path(period)) < state_mtime:
                return True                  # published before the last state write
        except OSError:
            return True                      # missing entirely
    return False


def _write_csv(path, rows):
    """Write rows to 'path' atomically. Returns False and logs on failure."""
    tmp = path + ".partial-%d" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(REPORT_COLUMNS))
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, path)               # readers never see a half-written file
        return True
    except (OSError, csv.Error) as exc:
        _discard(tmp)
        if path == report_state_path():     # this one is not allowed to fail quietly
            log("ERROR", "cannot write report state '%s' cause='%s'" % (path, why(exc)))
        return False


def _report_keep(row):
    """Keep a row not seen this run only while it is inside the retention."""
    if REPORT_KEEP_DAYS <= 0:
        return True
    stamp = row.get("moved_at") or row.get("first_seen") or ""
    try:
        age = datetime.now().astimezone() - datetime.fromisoformat(stamp).astimezone()
    except ValueError:
        return True                         # unparseable: keep rather than lose it
    return age.days < REPORT_KEEP_DAYS


# --------------------------------------------------------------------------- #
# Processing
# --------------------------------------------------------------------------- #
def process_pair(jf, df):
    """Dispatch one settled pair: jf is the .json sidecar, df the data file.

    A sequence of gates, each of which either logs a reason and returns
    (leaving both files untouched) or falls through to the next:
      file is a real, non-symlink file -> engine resolves it -> destination is
      usable -> move the data file -> archive the sidecar.

    The order matters at the end: the data file moves first and the sidecar is
    archived second, so an interruption leaves the sidecar behind rather than
    losing track of a file that has already moved.
    """
    global PROCESSED, INVALID, ERRORS, UNMATCHED
    jbase = os.path.basename(jf) if jf else ""
    dbase = os.path.basename(df)

    if os.path.islink(df):
        log("ERROR", "FAILURE move source='%s' reason='data file is a symlink' - skipped" % df)
        report_note(df, "failed", reason="data file is a symlink")
        ERRORS += 1
        return
    if not os.path.isfile(df):
        log("ERROR", "FAILURE move source='%s' reason='data file is not a regular file' - skipped" % df)
        report_note(df, "failed", reason="not a regular file")
        ERRORS += 1
        return

    fdate = _file_date(df)          # read now: a success moves the file away
    if DEBUG:
        log("DEBUG", "processing pair: source='%s' meta='%s'" % (df, jf or "(none)"))

    result = CFG.resolve(jf, DEBUG, system_fields(df))
    if DEBUG:
        for tline in result.get("debug", []):
            log("DEBUG", tline)

    # With no sidecar there is no jf to name in a log line; the data file is
    # the subject of the message either way.
    src = jf or df
    status = result["status"]
    if status == "INVALID":
        log("ERROR", "FAILURE move source='%s' reason='invalid JSON' cause='%s' - left in place"
            % (jf, result.get("cause", "")))                     # jf is always set here
        report_note(df, "failed", reason="invalid JSON: " + result.get("cause", ""))
        INVALID += 1
        return
    if status == "REQUIRED_FAIL":
        log("ERROR", "FAILURE move source='%s' reason='missing/empty required field(s): %s' - left in place"
            % (src, ", ".join(result["missing"])))
        report_note(df, "failed", reason="missing required field(s): " + ", ".join(result["missing"]))
        ERRORS += 1
        return
    if status == "NOMATCH":
        log("WARN", "no rule matched source='%s' (%s) - left in place" % (src, result["summary"]))
        report_note(df, "unmatched", reason="no rule matched")
        UNMATCHED += 1
        return
    if status == "UNSAFE":
        log("ERROR", "FAILURE move source='%s' dest='%s' reason='unsafe or empty destination' - left in place"
            % (df, result["dest"]))
        report_note(df, "failed", result["dest"], "unsafe or empty destination")
        ERRORS += 1
        return
    if status != "OK":
        log("ERROR", "FAILURE move source='%s' reason='resolver error' - left in place" % src)
        report_note(df, "failed", reason="resolver error")
        ERRORS += 1
        return

    dest, ruleno, ruletext = result["dest"], result["ruleno"], result["ruletext"]

    if DRY_RUN:
        problem = dest_problem(dest)
        if problem:
            log("ERROR", "FAILURE move source='%s' dest='%s' reason='%s' (rule #%s: %s) - left in place"
                % (df, dest, problem, ruleno, ruletext))
            ERRORS += 1
            return
        log("INFO", "SUCCESS move source='%s' dest='%s' target='%s' (rule #%s: %s) archived='%s'"
            % (df, dest, collision_safe(dest, dbase), ruleno, ruletext,
               collision_safe(JSON_ARCHIVE_DIR, jbase) if jf else "-"))
        PROCESSED += 1
        return

    if not os.path.isdir(dest):
        if not CREATE_DIRS:
            log("ERROR", "FAILURE move source='%s' dest='%s' reason='destination directory does not "
                         "exist (CREATE_DIRS is no)' (rule #%s: %s) - left in place"
                % (df, dest, ruleno, ruletext))
            report_note(df, "failed", dest, "destination directory does not exist")
            ERRORS += 1
            return
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as exc:
            log("ERROR", "FAILURE move source='%s' dest='%s' reason='cannot create destination' "
                         "cause='%s' (rule #%s: %s) - left in place"
                % (df, dest, why(exc), ruleno, ruletext))
            report_note(df, "failed", dest, "cannot create destination: " + why(exc))
            log("ERROR", "DIAG create dest='%s' %s"
                % (dest, diagnose(df, _nearest_existing(dest), exc)))
            ERRORS += 1
            return

    target = collision_safe(dest, dbase)
    step, exc = move_file(df, target)
    if step == "remove_source":
        # Delivered, but the source is still here: the next run would dispatch
        # it a second time. Say so plainly -- this one needs a human.
        log("ERROR", "FAILURE move source='%s' dest='%s' target='%s' reason='copied but the source "
                     "could not be removed, it will be dispatched again next run' step='%s' "
                     "cause='%s' (rule #%s: %s)"
            % (df, dest, target, step, why(exc), ruleno, ruletext))
        log("ERROR", "DIAG move %s" % diagnose(df, dest, exc))
        report_note(df, "failed", dest, "copied but the source could not be removed: " + why(exc))
        ERRORS += 1
        return
    if step:
        log("ERROR", "FAILURE move source='%s' dest='%s' reason='move failed' step='%s' cause='%s' "
                     "(rule #%s: %s) - left in place"
            % (df, dest, step, why(exc), ruleno, ruletext))
        log("ERROR", "DIAG move %s" % diagnose(df, dest, exc))
        report_note(df, "failed", dest, "%s: %s" % (step, why(exc)))
        ERRORS += 1
        return

    jtarget = "-"
    if jf:
        jtarget = collision_safe(JSON_ARCHIVE_DIR, jbase)
        jstep, jexc = move_file(jf, jtarget)
        if jstep:
            log("ERROR", "FAILURE archive source='%s' target='%s' reason='data moved but JSON archiving "
                         "failed' step='%s' cause='%s'" % (df, target, jstep, why(jexc)))
            report_note(df, "failed", dest, "sidecar archiving failed: " + why(jexc))
            log("ERROR", "DIAG archive %s" % diagnose(jf, JSON_ARCHIVE_DIR, jexc))
            ERRORS += 1
            return

    log("INFO", "SUCCESS move source='%s' dest='%s' target='%s' (rule #%s: %s) archived='%s'"
        % (df, dest, target, ruleno, ruletext, jtarget))
    report_note(df, "success", dest, file_date=fdate)
    PROCESSED += 1


def process_all():
    """Scan INCOMING_DIR, pair the files, let the pairs settle, dispatch them.

    A unit of work is <base>.json plus exactly one sibling <base>.<ext>. Both
    halves being present is what says the producer is done announcing a file,
    so anything unpaired is simply left for a later run: a sidecar with no data
    file yet, a data file whose sidecar has not arrived, and also the ambiguous
    case of several data files sharing one base name, which is reported rather
    than guessed at.
    """
    global INCOMPLETE, UNSTABLE
    if not os.path.isdir(INCOMING_DIR):
        log("ERROR", "incoming directory does not exist: '%s'" % INCOMING_DIR)
        return 1

    try:
        entries = os.listdir(INCOMING_DIR)
    except OSError as exc:
        log("ERROR", "cannot read incoming directory: '%s' cause='%s'" % (INCOMING_DIR, why(exc)))
        return 1

    pairs = []
    for jname in sorted(f for f in entries if f.endswith(".json")):
        jf = os.path.join(INCOMING_DIR, jname)
        base = jname[:-5]  # strip ".json"
        if not base:
            continue
        sibs = [os.path.join(INCOMING_DIR, f) for f in entries
                if f.startswith(base + ".") and f != jname
                and os.path.exists(os.path.join(INCOMING_DIR, f))]
        if len(sibs) == 0:
            log("INFO", "waiting for data file source='%s' (incomplete pair) - left in place" % jf)
            INCOMPLETE += 1
            continue
        if len(sibs) > 1:
            log("WARN", "ambiguous source='%s' reason='several data files match same base name' - left in place" % jf)
            INCOMPLETE += 1
            continue
        pairs.append((jf, sibs[0]))

    # Data files with no .json sidecar. Normally they are simply not ready --
    # the producer announces a file by writing both halves -- so they wait for
    # a later run. With DISPATCH_WITHOUT_JSON they become units of work of
    # their own instead, matched on their system metadata alone ($Filename,
    # $Filesize, $Filedatetime) and moved with nothing to archive.
    for f in sorted(entries):
        p = os.path.join(INCOMING_DIR, f)
        if not os.path.isfile(p) or f.endswith(".json"):
            continue
        base_f = f.rsplit(".", 1)[0] if "." in f else f
        if os.path.exists(os.path.join(INCOMING_DIR, base_f + ".json")):
            continue
        if DISPATCH_WITHOUT_JSON:
            pairs.append((None, p))
            continue
        log("INFO", "waiting for metadata source='%s' reason='no .json sidecar yet' - left in place" % p)
        report_note(p, "pending", reason="no .json sidecar yet")
        INCOMPLETE += 1

    if not pairs:
        return 0

    # Stability / IO gate. A file still being copied in must not be moved: the
    # move would truncate it, and across filesystems the tail would be lost.
    # Two independent checks, because each has a blind spot the other covers:
    #   1. size+mtime before and after a pause -- catches a file visibly growing,
    #      but not a slow or buffered producer that happens to be idle just now
    #   2. is anyone holding it open for writing -- catches exactly that case
    # One sleep for the whole batch, not one per file, and one open-files scan
    # for all paths at once; both are snapshots of the same instant.
    before = [(_sig(jf) if jf else None, _sig(df)) for (jf, df) in pairs]
    if STABLE_SECONDS > 0:
        time.sleep(STABLE_SECONDS)
    busy = paths_open_for_write([p for pair in pairs for p in pair if p])
    for (jf, df), sig0 in zip(pairs, before):
        if (_sig(jf) if jf else None, _sig(df)) != sig0:
            log("INFO", "still changing source='%s' - will retry next run" % df)
            report_note(df, "pending", reason="still being written")
            UNSTABLE += 1
            continue
        if jf in busy or df in busy:
            log("INFO", "still changing source='%s' reason='open for writing' - will retry next run" % df)
            report_note(df, "pending", reason="open for writing")
            UNSTABLE += 1
            continue
        process_pair(jf, df)
    return 0


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #
def resolve_config_path(args):
    if args.config_file:
        return args.config_file
    if args.config:
        return args.config
    env = os.environ.get("DISPATCH_CONFIG")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dispatch.conf")


def maybe_reexec(config_path):
    """Honor the config's PYTHON setting by re-executing with that interpreter.

    A bad PYTHON path is a hard error (exit 3), mirroring the old behavior.

    This runs before anything else, and parses the config a second time on its
    own: the interpreter has to be chosen before the real startup, and that
    choice lives in the very file we are about to read properly. The
    _DISPATCH_REEXEC marker in the environment is what stops the replacement
    process from doing it all over again -- os.execv keeps the environment, so
    without it a PYTHON pointing at a different-but-equivalent path would loop.
    """
    if os.environ.get("_DISPATCH_REEXEC"):
        return
    cfg = engine.Config()
    cfg.parse(config_path)
    py = cfg.settings.get("PYTHON", "")
    if not py:
        return
    if not (os.path.isfile(py) and os.access(py, os.X_OK)):
        sys.stderr.write("%s [ERROR] python3 interpreter not usable: '%s'\n"
                         % (datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"), py))
        sys.exit(3)
    try:
        same = os.path.samefile(py, sys.executable)
    except OSError:
        same = os.path.realpath(py) == os.path.realpath(sys.executable)
    if not same:
        os.environ["_DISPATCH_REEXEC"] = "1"
        os.execv(py, [py, os.path.abspath(__file__)] + sys.argv[1:])


def build_parser():
    p = argparse.ArgumentParser(
        prog="dispatch.sh",
        description="Route incoming data files by their JSON sidecar metadata.",
        epilog=("Config resolution (first wins): --config-file > $DISPATCH_CONFIG > "
                "dispatch.conf next to the script.  "
                "Python interpreter: config PYTHON setting > $DISPATCH_PYTHON > python3."),
    )
    p.add_argument("config", nargs="?", help="path to the config file (positional)")
    p.add_argument("--config-file", dest="config_file", metavar="FILE", help="path to the config file")
    # default=None means "not given", which is what lets the config decide.
    # Each flag has a --no- twin so the command line can override a config that
    # turns the option on, not only one that leaves it off.
    p.add_argument("--dry-run", "-n", dest="dry_run", action="store_true", default=None,
                   help="log what would happen, but move nothing (overrides DRY_RUN in the config)")
    p.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                   help="move files even if the config sets DRY_RUN = yes")
    p.add_argument("--debug", "-d", dest="debug", action="store_true", default=None,
                   help="verbose trace: field values, variables, rule resolution (overrides DEBUG)")
    p.add_argument("--no-debug", dest="debug", action="store_false",
                   help="stay quiet even if the config sets DEBUG = yes")
    p.add_argument("--check", action="store_true", help="validate the config file and exit (0 = OK)")
    return p


def main(argv):
    global INCOMING_DIR, JSON_ARCHIVE_DIR, LOG_DIR, STABLE_SECONDS, LOG_FILE, ERROR_LOG
    global DRY_RUN, DEBUG, CFG, ERRORS, CREATE_DIRS, DISPATCH_WITHOUT_JSON
    global LOG_MAX_BYTES, LOG_KEEP, REPORT_DIR, REPORT_KEEP_DAYS, REPORT_SPLIT

    args = build_parser().parse_args(argv)
    _DEST_CHECK_CACHE.clear()
    # Provisional: only the flags are known before the config is read. The
    # config's own DRY_RUN / DEBUG are folded in below, once it is parsed.
    DRY_RUN = args.dry_run is True
    DEBUG = args.debug is True
    config_path = resolve_config_path(args)

    maybe_reexec(config_path)

    # Enforce the minimum interpreter version (checked on the FINAL interpreter,
    # i.e. after any PYTHON re-exec above).
    if sys.version_info < MIN_PYTHON:
        sys.stderr.write(
            "%s [ERROR] file-dispatch requires Python %s or newer (running %s)\n"
            % (datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
               ".".join(map(str, MIN_PYTHON)),
               ".".join(map(str, sys.version_info[:3]))))
        return 3

    # Preflight: parse + validate the config.
    CFG = engine.Config()
    CFG.parse(config_path)
    CFG.validate()

    INCOMING_DIR = CFG.settings.get("INCOMING_DIR", "")
    JSON_ARCHIVE_DIR = CFG.settings.get("JSON_ARCHIVE_DIR", "")
    LOG_DIR = CFG.settings.get("LOG_DIR", "")
    if LOG_DIR:
        LOG_FILE = os.path.join(LOG_DIR, "dispatch.log")
        ERROR_LOG = os.path.join(LOG_DIR, "errors.log")
    try:
        STABLE_SECONDS = int(CFG.settings.get("STABLE_SECONDS", "2"))
    except ValueError:
        STABLE_SECONDS = 2
    try:
        LOG_MAX_BYTES = int(CFG.settings.get("LOG_MAX_MB", "10")) * 1024 * 1024
        LOG_KEEP = int(CFG.settings.get("LOG_KEEP", "5"))
    except ValueError:                      # validate() reports it; keep the defaults
        pass
    REPORT_DIR = CFG.settings.get("REPORT_DIR", "")
    REPORT_SPLIT = CFG.settings.get("REPORT_SPLIT", "none").strip().lower()
    if REPORT_SPLIT not in engine.REPORT_SPLITS:
        REPORT_SPLIT = "none"               # validate() reports it
    try:
        REPORT_KEEP_DAYS = int(CFG.settings.get("REPORT_KEEP_DAYS", "90"))
    except ValueError:
        pass
    CREATE_DIRS = engine.setting_bool(CFG.settings, "CREATE_DIRS")
    DISPATCH_WITHOUT_JSON = engine.setting_bool(CFG.settings, "DISPATCH_WITHOUT_JSON")
    # The command line wins: the config is consulted only where no flag was
    # given (args.<name> is None), so --dry-run and --no-dry-run both override
    # whatever the file says.
    if args.dry_run is None:
        DRY_RUN = engine.setting_bool(CFG.settings, "DRY_RUN")
    if args.debug is None:
        DEBUG = engine.setting_bool(CFG.settings, "DEBUG")

    if CFG.errors:
        if LOG_DIR:
            try:
                os.makedirs(LOG_DIR, exist_ok=True)
            except OSError:
                pass
        for w in CFG.warnings:
            log("WARN", "config: " + w)
        for e in CFG.errors:
            log("ERROR", "config: " + e)
        return 2

    if args.check:
        print("config OK: %s" % config_path, file=sys.stderr)
        return 0

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        pass
    for w in CFG.warnings:
        log("WARN", "config: " + w)

    if DRY_RUN:
        log("INFO", "mode: no files will be moved")
        for label, d in (("JSON_ARCHIVE_DIR", JSON_ARCHIVE_DIR), ("LOG_DIR", LOG_DIR)):
            problem = dest_problem(d)
            if problem:
                log("ERROR", "%s '%s': %s" % (label, d, problem))
                ERRORS += 1
    else:
        try:
            os.makedirs(JSON_ARCHIVE_DIR, exist_ok=True)
        except OSError:
            pass
        # One run at a time. A batch that takes longer than the cron interval
        # would otherwise have the next tick processing the same pairs: both
        # runs resolve the same file, and one of the two moves fail. The lock
        # is non-blocking on purpose -- a second run exits quietly (status 0,
        # this is normal, not an error) rather than queueing up behind the
        # first. It is released when the process ends, whichever way it ends,
        # so no stale lock file can wedge the next run.
        lock_path = os.path.join(LOG_DIR, ".dispatch.lock")
        try:
            lock_fd = open(lock_path, "w")
        except OSError as exc:
            log("ERROR", "cannot open lock file '%s' cause='%s'" % (lock_path, why(exc)))
            return 1
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("INFO", "another instance is already running - exiting")
            return 0
        # keep lock_fd open for the lifetime of the process

    if not DRY_RUN:
        report_load()
    rc = process_all()
    if not DRY_RUN:
        report_save()

    log("INFO", "run summary: processed=%d unmatched=%d invalid=%d incomplete=%d unstable=%d errors=%d"
        % (PROCESSED, UNMATCHED, INVALID, INCOMPLETE, UNSTABLE, ERRORS))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
