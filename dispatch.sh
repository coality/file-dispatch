#!/usr/bin/env bash
#
# file-dispatch - route incoming data files to destination directories based on
# the metadata carried by their JSON sidecar, driven by a single config file.
#
# This script is the ORCHESTRATOR: CLI, cron lock, file pairing, I/O stability,
# moving files and logging. All the fiddly parts -- parsing the config DSL, the
# rule grammar, variable expansion and matching a JSON file against the rules --
# live in the Python engine (engine.py) next to this script.
#
# Usage:
#   dispatch.sh [--config-file FILE] [--dry-run] [--debug] [--check]
#     --config-file FILE  path to the config file
#     --dry-run, -n       log what would happen, but move nothing
#     --debug, -d         verbose trace: field values, variables, rule resolution
#     --check             validate the config and exit (0 = OK)
#     --help, -h          show help
#   Config resolution:      --config-file  >  $DISPATCH_CONFIG  >  dispatch.conf next to script
#   Python interpreter:     config "PYTHON" setting  >  $DISPATCH_PYTHON  >  python3
#
# Dependencies: bash >= 4, python3 (>= 3.6), flock, coreutils (mv/mkdir/stat/date/sleep).
#
# `set -e` is intentionally NOT used: the script does explicit error handling on
# every filesystem operation, so it can never exit half-way through a move.

set -uo pipefail
shopt -s nullglob

_src=${BASH_SOURCE[0]}
_dir=${_src%/*}
[[ $_dir == "$_src" ]] && _dir="."
SCRIPT_DIR="$(cd -- "$_dir" && pwd)"
unset _src _dir
ENGINE="$SCRIPT_DIR/engine.py"

# Settings (filled from the engine's "load" output)
INCOMING_DIR=""
JSON_ARCHIVE_DIR=""
LOG_DIR=""
STABLE_SECONDS="2"
REQUIRED_STR=""

# Derived from LOG_DIR
LOG_FILE=""     # $LOG_DIR/dispatch.log  (all actions)
ERROR_LOG=""    # $LOG_DIR/errors.log    (WARN + ERROR only)

PY="python3"
CHECK_ONLY=0
DRY_RUN=0
DEBUG=0
CONFIG_PATH=""

# Run counters
PROCESSED=0; UNMATCHED=0; INVALID=0; INCOMPLETE=0; UNSTABLE=0; ERRORS=0

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
sanitize() {
    local s=$1
    s=${s//$'\n'/ }; s=${s//$'\r'/ }; s=${s//$'\t'/ }
    s=${s//$'\033'/?}; s=${s//$'\a'/}; s=${s//$'\b'/}; s=${s//$'\f'/}; s=${s//$'\v'/}
    printf '%s' "$s"
}

log() {
    local level=$1; shift
    local msg; msg=$(sanitize "$*")
    (( DRY_RUN == 1 )) && msg="DRY-RUN $msg"
    local ts; ts=$(date '+%Y-%m-%dT%H:%M:%S%z')
    local line="$ts [$level] $msg"
    if [[ -n ${LOG_FILE:-} ]]; then
        printf '%s\n' "$line" >>"$LOG_FILE" 2>/dev/null || true
    fi
    if [[ $level == ERROR || $level == WARN ]]; then
        [[ -n ${ERROR_LOG:-} ]] && printf '%s\n' "$line" >>"$ERROR_LOG" 2>/dev/null || true
        printf '%s\n' "$line" >&2
    elif [[ $level == DEBUG ]]; then
        printf '%s\n' "$line" >&2
    fi
}

# Debug trace (no-op unless --debug): goes to dispatch.log and stderr.
dbg() { (( DEBUG == 1 )) && log DEBUG "$*"; }

# --------------------------------------------------------------------------- #
# Minimal bootstrap: find which python interpreter to run the engine with.
# (A full config parse is the engine's job; here we only need PYTHON.)
# --------------------------------------------------------------------------- #
extract_setting() {
    local name=$1 file=$2 line val
    [[ -r $file ]] || return 1
    while IFS= read -r line || [[ -n $line ]]; do
        line=${line#"${line%%[![:space:]]*}"}   # strip leading whitespace
        case $line in
            "$name"=*|"$name"[[:space:]]*=*) ;;
            *) continue ;;
        esac
        val=${line#*=}
        val=${val#"${val%%[![:space:]]*}"}       # strip leading whitespace
        if [[ $val == '"'* ]]; then
            val=${val#\"}; val=${val%%\"*}
        elif [[ $val == "'"* ]]; then
            val=${val#\'}; val=${val%%\'*}
        else
            val=${val%%[[:space:]#]*}             # bare: up to whitespace or #
        fi
        printf '%s' "$val"
        return 0
    done < "$file"
    return 1
}

# --------------------------------------------------------------------------- #
# Filesystem helpers
# --------------------------------------------------------------------------- #
collision_safe_path() {
    local dir=$1 name=$2
    local path="$dir/$name"
    if [[ -e $path ]]; then
        local ts; ts=$(date '+%Y%m%d-%H%M%S')
        path="$dir/${name}.${ts}"
        [[ -e $path ]] && path="$dir/${name}.${ts}.$$"
    fi
    printf '%s' "$path"
}

is_open_for_write() {
    command -v lsof >/dev/null 2>&1 || return 1
    local line
    while IFS= read -r line; do
        [[ $line == 'au' || $line == 'aw' ]] && return 0
    done < <(lsof -F a -- "$1" 2>/dev/null)
    return 1
}

# --------------------------------------------------------------------------- #
# Processing one complete, stable pair (delegates resolution to the engine)
# --------------------------------------------------------------------------- #
process_pair() {
    local jf=$1 df=$2
    local jbase=${jf##*/}
    local dbase=${df##*/}

    if [[ -L $df ]]; then
        log WARN "FAILURE source='$df' reason='data file is a symlink' - skipped"; ERRORS=$((ERRORS+1)); return
    fi
    if [[ ! -f $df ]]; then
        log WARN "FAILURE source='$df' reason='data file is not a regular file' - skipped"; ERRORS=$((ERRORS+1)); return
    fi

    (( DEBUG == 1 )) && dbg "processing pair: source='$df' meta='$jf'"

    # Ask the engine to resolve this JSON against the config.
    local status="" dest="" ruleno="" ruletext="" missing="" summary=""
    local key val
    local dbgargs=()
    (( DEBUG == 1 )) && dbgargs=(--debug)
    while IFS=$'\t' read -r key val; do
        case $key in
            D)        (( DEBUG == 1 )) && log DEBUG "$val" ;;
            status)   status=$val ;;
            dest)     dest=$val ;;
            ruleno)   ruleno=$val ;;
            ruletext) ruletext=$val ;;
            missing)  missing=$val ;;
            summary)  summary=$val ;;
        esac
    done < <("$PY" "$ENGINE" resolve "$CONFIG_PATH" "$jf" "${dbgargs[@]}")

    case $status in
        INVALID)
            log ERROR "FAILURE source='$jf' reason='invalid JSON' - left in place"
            INVALID=$((INVALID+1)); return ;;
        REQUIRED_FAIL)
            log ERROR "FAILURE source='$jf' reason='missing/empty required field(s): ${missing// /, }' - left in place"
            ERRORS=$((ERRORS+1)); return ;;
        NOMATCH)
            log WARN "no rule matched source='$jf' ($summary) - left in place"
            UNMATCHED=$((UNMATCHED+1)); return ;;
        UNSAFE)
            log ERROR "FAILURE source='$df' dest='$dest' reason='unsafe or empty destination (contains ..)' - left in place"
            ERRORS=$((ERRORS+1)); return ;;
        OK) ;;
        *)
            log ERROR "FAILURE source='$jf' reason='resolver error' - left in place"
            ERRORS=$((ERRORS+1)); return ;;
    esac

    if (( DRY_RUN == 1 )); then
        local wtarget; wtarget=$(collision_safe_path "$dest" "$dbase")
        log INFO "would move source='$df' dest='$dest' target='$wtarget' (rule #$ruleno: $ruletext); would archive '$jbase'"
        PROCESSED=$((PROCESSED+1)); return
    fi

    if ! mkdir -p -- "$dest" 2>/dev/null; then
        log ERROR "FAILURE source='$df' dest='$dest' reason='cannot create destination' - left in place"
        ERRORS=$((ERRORS+1)); return
    fi

    local target; target=$(collision_safe_path "$dest" "$dbase")
    if ! mv -- "$df" "$target" 2>/dev/null; then
        log ERROR "FAILURE source='$df' dest='$dest' reason='move failed' - left in place"
        ERRORS=$((ERRORS+1)); return
    fi

    local jtarget; jtarget=$(collision_safe_path "$JSON_ARCHIVE_DIR" "$jbase")
    if ! mv -- "$jf" "$jtarget" 2>/dev/null; then
        log ERROR "FAILURE source='$df' target='$target' reason='data moved but JSON archiving failed'"
        ERRORS=$((ERRORS+1)); return
    fi

    log INFO "SUCCESS source='$df' dest='$dest' target='$target' (rule #$ruleno: $ruletext) archived='$jtarget'"
    PROCESSED=$((PROCESSED+1))
}

process_all() {
    if [[ ! -d $INCOMING_DIR ]]; then
        log ERROR "incoming directory does not exist: '$INCOMING_DIR'"
        return 1
    fi

    local jsons=("$INCOMING_DIR"/*.json)
    local pj=() pd=()
    local jf
    for jf in "${jsons[@]}"; do
        local base=${jf##*/}; base=${base%.json}
        [[ -z $base ]] && continue
        local candidates=("$INCOMING_DIR/$base".*)
        local data="" count=0 c
        for c in "${candidates[@]}"; do
            [[ $c == "$jf" ]] && continue
            [[ -e $c ]] || continue
            data=$c; count=$((count+1))
        done
        if (( count == 0 )); then
            log INFO "waiting for data file source='$jf' (incomplete pair) - left in place"
            INCOMPLETE=$((INCOMPLETE+1)); continue
        elif (( count > 1 )); then
            log WARN "ambiguous source='$jf' reason='several data files match same base name' - left in place"
            INCOMPLETE=$((INCOMPLETE+1)); continue
        fi
        pj+=("$jf"); pd+=("$data")
    done

    # Data files still waiting for their .json sidecar
    local f
    for f in "$INCOMING_DIR"/*; do
        [[ -f $f ]] || continue
        [[ $f == *.json ]] && continue
        local b=${f##*/}
        [[ -e "$INCOMING_DIR/${b%.*}.json" ]] && continue
        log INFO "waiting for metadata source='$f' reason='no .json sidecar yet' - left in place"
        INCOMPLETE=$((INCOMPLETE+1))
    done

    if (( ${#pj[@]} == 0 )); then
        return 0
    fi

    # Stability / IO gate: snapshot, wait once, re-check
    local sig_before=() idx
    for idx in "${!pj[@]}"; do
        sig_before[$idx]="$(stat -c '%s:%Y' -- "${pj[$idx]}" 2>/dev/null):$(stat -c '%s:%Y' -- "${pd[$idx]}" 2>/dev/null)"
    done
    if (( STABLE_SECONDS > 0 )); then
        sleep "$STABLE_SECONDS"
    fi
    for idx in "${!pj[@]}"; do
        local now
        now="$(stat -c '%s:%Y' -- "${pj[$idx]}" 2>/dev/null):$(stat -c '%s:%Y' -- "${pd[$idx]}" 2>/dev/null)"
        if [[ $now != "${sig_before[$idx]}" ]]; then
            log INFO "still changing source='${pd[$idx]}' - will retry next run"
            UNSTABLE=$((UNSTABLE+1)); continue
        fi
        if is_open_for_write "${pj[$idx]}" || is_open_for_write "${pd[$idx]}"; then
            log INFO "still changing source='${pd[$idx]}' reason='open for writing' - will retry next run"
            UNSTABLE=$((UNSTABLE+1)); continue
        fi
        process_pair "${pj[$idx]}" "${pd[$idx]}"
    done
}

# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
usage() {
    printf '%s\n' \
        'Usage: dispatch.sh [--config-file FILE] [--dry-run] [--debug] [--check]' \
        '' \
        'Options:' \
        '  --config-file FILE   path to the config file' \
        '  --dry-run, -n        log what would happen, but move nothing' \
        '  --debug, -d          verbose trace: field values, variables, rule resolution' \
        '  --check              validate the config file and exit (0 = OK)' \
        '  --help, -h           show this help' \
        '' \
        'Config file resolution (first match wins):' \
        '  1. --config-file FILE   2. $DISPATCH_CONFIG   3. dispatch.conf next to the script' \
        'Python interpreter:  config "PYTHON" setting  >  $DISPATCH_PYTHON  >  python3'
}

now_ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

main() {
    while (( $# )); do
        case $1 in
            --check) CHECK_ONLY=1 ;;
            --dry-run|-n) DRY_RUN=1 ;;
            --debug|-d) DEBUG=1 ;;
            --config-file)
                shift
                [[ $# -gt 0 ]] || { echo "--config-file requires a path" >&2; exit 2; }
                CONFIG_PATH=$1 ;;
            --config-file=*) CONFIG_PATH=${1#*=} ;;
            -h|--help) usage; exit 0 ;;
            --) shift; [[ $# -gt 0 ]] && CONFIG_PATH=$1; break ;;
            -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
            *) CONFIG_PATH=$1 ;;
        esac
        shift
    done
    [[ -z $CONFIG_PATH ]] && CONFIG_PATH=${DISPATCH_CONFIG:-}
    [[ -z $CONFIG_PATH ]] && CONFIG_PATH="$SCRIPT_DIR/dispatch.conf"

    # Pick the python interpreter: config PYTHON > $DISPATCH_PYTHON > python3.
    local from_cfg=""
    from_cfg=$(extract_setting PYTHON "$CONFIG_PATH") || from_cfg=""
    PY=${from_cfg:-${DISPATCH_PYTHON:-python3}}

    if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info[0] >= 3 else 1)' >/dev/null 2>&1; then
        echo "$(now_ts) [ERROR] python3 interpreter not usable: '$PY'" >&2
        exit 3
    fi
    if [[ ! -r $ENGINE ]]; then
        echo "$(now_ts) [ERROR] engine not found: '$ENGINE'" >&2
        exit 3
    fi

    # Preflight: the engine parses + validates the config and reports settings.
    local status="FAIL" tag a b
    local cfg_errors=() cfg_warnings=()
    while IFS=$'\t' read -r tag a b; do
        case $tag in
            SET)
                case $a in
                    INCOMING_DIR)     INCOMING_DIR=$b ;;
                    JSON_ARCHIVE_DIR) JSON_ARCHIVE_DIR=$b ;;
                    LOG_DIR)          LOG_DIR=$b ;;
                    STABLE_SECONDS)   STABLE_SECONDS=$b ;;
                    REQUIRED)         REQUIRED_STR=$b ;;
                esac ;;
            WARN)   cfg_warnings+=("$a") ;;
            ERR)    cfg_errors+=("$a") ;;
            STATUS) status=$a ;;
        esac
    done < <("$PY" "$ENGINE" load "$CONFIG_PATH")

    if [[ -n $LOG_DIR ]]; then
        LOG_FILE="$LOG_DIR/dispatch.log"
        ERROR_LOG="$LOG_DIR/errors.log"
    fi

    if [[ $status != OK || ${#cfg_errors[@]} -gt 0 ]]; then
        [[ -n $LOG_DIR ]] && mkdir -p -- "$LOG_DIR" 2>/dev/null || true
        local w e
        for w in "${cfg_warnings[@]}"; do log WARN "config: $w"; done
        for e in "${cfg_errors[@]}"; do log ERROR "config: $e"; done
        exit 2
    fi

    if (( CHECK_ONLY == 1 )); then
        echo "config OK: $CONFIG_PATH" >&2
        exit 0
    fi

    mkdir -p -- "$LOG_DIR" 2>/dev/null || true
    local w
    for w in "${cfg_warnings[@]}"; do log WARN "config: $w"; done

    if (( DRY_RUN == 1 )); then
        log INFO "mode: no files will be moved"
    else
        mkdir -p -- "$JSON_ARCHIVE_DIR" 2>/dev/null || true
        local lock="$LOG_DIR/.dispatch.lock"
        if ! exec 9>"$lock"; then
            log ERROR "cannot open lock file '$lock'"; exit 1
        fi
        if ! flock -n 9; then
            log INFO "another instance is already running - exiting"
            exit 0
        fi
    fi

    process_all
    local rc=$?

    log INFO "run summary: processed=$PROCESSED unmatched=$UNMATCHED invalid=$INVALID incomplete=$INCOMPLETE unstable=$UNSTABLE errors=$ERRORS"
    exit "$rc"
}

main "$@"
