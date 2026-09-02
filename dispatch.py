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
import fcntl
import os
import shutil
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


def _append(path, line):
    try:
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
    if level in ("ERROR", "WARN"):
        if ERROR_LOG:
            _append(ERROR_LOG, line)
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


def _sig(path):
    try:
        st = os.stat(path)
        return (st.st_size, int(st.st_mtime))
    except OSError:
        return None


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
        log("WARN", "FAILURE move source='%s' reason='data file is a symlink' - skipped" % df)
        ERRORS += 1
        return
    if not os.path.isfile(df):
        log("WARN", "FAILURE move source='%s' reason='data file is not a regular file' - skipped" % df)
        ERRORS += 1
        return

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
        log("ERROR", "FAILURE move source='%s' reason='invalid JSON' - left in place" % jf)  # jf is set here
        INVALID += 1
        return
    if status == "REQUIRED_FAIL":
        log("ERROR", "FAILURE move source='%s' reason='missing/empty required field(s): %s' - left in place"
            % (src, ", ".join(result["missing"])))
        ERRORS += 1
        return
    if status == "NOMATCH":
        log("WARN", "no rule matched source='%s' (%s) - left in place" % (src, result["summary"]))
        UNMATCHED += 1
        return
    if status == "UNSAFE":
        log("ERROR", "FAILURE move source='%s' dest='%s' reason='unsafe or empty destination' - left in place"
            % (df, result["dest"]))
        ERRORS += 1
        return
    if status != "OK":
        log("ERROR", "FAILURE move source='%s' reason='resolver error' - left in place" % src)
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
            ERRORS += 1
            return
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError:
            log("ERROR", "FAILURE move source='%s' dest='%s' reason='cannot create destination' "
                         "(rule #%s: %s) - left in place" % (df, dest, ruleno, ruletext))
            ERRORS += 1
            return

    target = collision_safe(dest, dbase)
    try:
        shutil.move(df, target)
    except (OSError, shutil.Error):
        log("ERROR", "FAILURE move source='%s' dest='%s' reason='move failed' (rule #%s: %s) - left in place"
            % (df, dest, ruleno, ruletext))
        ERRORS += 1
        return

    jtarget = "-"
    if jf:
        jtarget = collision_safe(JSON_ARCHIVE_DIR, jbase)
        try:
            shutil.move(jf, jtarget)
        except (OSError, shutil.Error):
            log("ERROR", "FAILURE archive source='%s' target='%s' reason='data moved but JSON archiving "
                         "failed'" % (df, target))
            ERRORS += 1
            return

    log("INFO", "SUCCESS move source='%s' dest='%s' target='%s' (rule #%s: %s) archived='%s'"
        % (df, dest, target, ruleno, ruletext, jtarget))
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
    except OSError:
        log("ERROR", "cannot read incoming directory: '%s'" % INCOMING_DIR)
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
            UNSTABLE += 1
            continue
        if jf in busy or df in busy:
            log("INFO", "still changing source='%s' reason='open for writing' - will retry next run" % df)
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
    p.add_argument("--dry-run", "-n", action="store_true", help="log what would happen, but move nothing")
    p.add_argument("--debug", "-d", action="store_true", help="verbose trace: field values, variables, rule resolution")
    p.add_argument("--check", action="store_true", help="validate the config file and exit (0 = OK)")
    return p


def main(argv):
    global INCOMING_DIR, JSON_ARCHIVE_DIR, LOG_DIR, STABLE_SECONDS, LOG_FILE, ERROR_LOG
    global DRY_RUN, DEBUG, CFG, ERRORS, CREATE_DIRS, DISPATCH_WITHOUT_JSON

    args = build_parser().parse_args(argv)
    _DEST_CHECK_CACHE.clear()
    DRY_RUN = args.dry_run
    DEBUG = args.debug
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
    CREATE_DIRS = engine.BOOLS.get(CFG.settings.get("CREATE_DIRS", "no").strip().lower(), False)
    DISPATCH_WITHOUT_JSON = engine.BOOLS.get(
        CFG.settings.get("DISPATCH_WITHOUT_JSON", "no").strip().lower(), False)

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
        except OSError:
            log("ERROR", "cannot open lock file '%s'" % lock_path)
            return 1
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("INFO", "another instance is already running - exiting")
            return 0
        # keep lock_fd open for the lifetime of the process

    rc = process_all()

    log("INFO", "run summary: processed=%d unmatched=%d invalid=%d incomplete=%d unstable=%d errors=%d"
        % (PROCESSED, UNMATCHED, INVALID, INCOMPLETE, UNSTABLE, ERRORS))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
