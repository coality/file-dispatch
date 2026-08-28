#!/usr/bin/env bash
#
# Self-contained end-to-end test suite for file-dispatch.
# Each test builds an isolated sandbox, writes a dispatch.conf, drops files in
# the incoming directory, runs dispatch.sh, and asserts the final state
# (file locations + log contents + exit code). No external test framework.
#
#   ./tests/run_tests.sh            run everything
#
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DISPATCH="$HERE/../dispatch.sh"

PASS=0
FAIL=0
declare -a FAILURES=()
CURRENT=""

# sandbox variables (reset by new_sandbox)
SB="" IN="" ARCH="" OUT="" LOGDIR="" LOGF="" ERRF="" CONF="" RC=0
STABLE=0

RED=$'\033[31m'; GREEN=$'\033[32m'; BOLD=$'\033[1m'; RESET=$'\033[0m'

# --------------------------------------------------------------------------- #
# Harness helpers
# --------------------------------------------------------------------------- #
new_sandbox() {
    SB=$(mktemp -d)
    IN="$SB/incoming"; ARCH="$SB/archive"; OUT="$SB/out"
    LOGDIR="$SB/logs"; LOGF="$LOGDIR/dispatch.log"; ERRF="$LOGDIR/errors.log"; CONF="$SB/dispatch.conf"
    mkdir -p "$IN" "$OUT" "$LOGDIR"
}

cleanup() { [[ -n $SB && -d $SB ]] && rm -rf "$SB"; SB=""; }

# write a config: base settings + the given variables/rules block
write_conf() {
    {
        printf 'INCOMING_DIR = "%s"\n' "$IN"
        printf 'JSON_ARCHIVE_DIR = "%s"\n' "$ARCH"
        printf 'LOG_DIR = "%s"\n' "$LOGDIR"
        printf 'STABLE_SECONDS = %s\n' "${STABLE:-0}"
        printf 'OUT = "%s"\n' "$OUT"
        printf '%s\n' "$1"
    } > "$CONF"
}

# create a data file ($1.$2) and its json sidecar ($1.json = $3); data = $4|"DATA"
mkpair() {
    printf '%s' "${4:-DATA}" > "$IN/$1.$2"
    printf '%s' "$3" > "$IN/$1.json"
}

run_dispatch() { "$DISPATCH" "$CONF" >/dev/null 2>"$SB/stderr"; RC=$?; }
run_dispatch_dry() { "$DISPATCH" --dry-run "$CONF" >/dev/null 2>"$SB/stderr"; RC=$?; }

# --------------------------------------------------------------------------- #
# Assertions
# --------------------------------------------------------------------------- #
ok() { PASS=$((PASS+1)); printf '    %sok%s   %s\n' "$GREEN" "$RESET" "$1"; }
ko() { FAIL=$((FAIL+1)); FAILURES+=("[$CURRENT] $1"); printf '    %sFAIL%s %s\n' "$RED" "$RESET" "$1"; }

a_exists()  { [[ -e $1 ]] && ok "exists: ${1#"$SB"/}" || ko "missing: ${1#"$SB"/}"; }
a_absent()  { [[ ! -e $1 ]] && ok "absent: ${1#"$SB"/}" || ko "should be gone: ${1#"$SB"/}"; }
a_log()     { grep -qF -- "$1" "$LOGF" 2>/dev/null && ok "log has: $1" || ko "log missing: $1"; }
a_log_re()  { grep -qE -- "$1" "$LOGF" 2>/dev/null && ok "log ~ /$1/" || ko "log no match: /$1/"; }
a_rc()      { [[ $RC == "$1" ]] && ok "exit=$1" || ko "exit expected $1, got $RC"; }
a_true()    { if eval "$1"; then ok "$2"; else ko "$2"; fi; }

no_files_under() { [[ -z "$(find "$1" -type f 2>/dev/null)" ]]; }

# --------------------------------------------------------------------------- #
# Test cases
# --------------------------------------------------------------------------- #
t01() {
    CURRENT="01 simple match -> move + archive + log(rule)"; new_sandbox
    write_conf '$category = "report" => "$OUT/reports"'
    mkpair example xml '{"category":"report","group":"alpha"}'
    run_dispatch
    a_exists "$OUT/reports/example.xml"
    a_exists "$ARCH/example.json"
    a_absent "$IN/example.xml"
    a_absent "$IN/example.json"
    a_log "SUCCESS source="
    a_log '(rule #6: $category = "report" => "$OUT/reports")'
    cleanup
}

t02() {
    CURRENT="02 variable composition"; new_sandbox
    write_conf 'GROUP = "$group"
$category = "report" => "$OUT/$GROUP/reports"'
    mkpair ex xml '{"category":"report","group":"B"}'
    run_dispatch
    a_exists "$OUT/B/reports/ex.xml"
    cleanup
}

t03() {
    CURRENT="03 field value routes to different dirs"; new_sandbox
    write_conf 'GROUP = "$group"
$category = "report" => "$OUT/$GROUP/reports"'
    mkpair a xml '{"category":"report","group":"X"}'
    mkpair b xml '{"category":"report","group":"Y"}'
    run_dispatch
    a_exists "$OUT/X/reports/a.xml"
    a_exists "$OUT/Y/reports/b.xml"
    cleanup
}

t04() {
    CURRENT="04 first matching rule wins"; new_sandbox
    write_conf '$category = "report" => "$OUT/first"
$category = "report" => "$OUT/second"'
    mkpair a xml '{"category":"report"}'
    run_dispatch
    a_exists "$OUT/first/a.xml"
    a_absent "$OUT/second/a.xml"
    cleanup
}

t05() {
    CURRENT="05 AND (one condition false)"; new_sandbox
    write_conf '$type = "export" AND $status = "new" => "$OUT/exp"'
    mkpair a xml '{"type":"export","status":"old"}'
    mkpair b xml '{"type":"export","status":"new"}'
    run_dispatch
    a_exists "$IN/a.xml"
    a_exists "$OUT/exp/b.xml"
    cleanup
}

t06() {
    CURRENT="06 wildcard '*' = any value"; new_sandbox
    write_conf '$anything = "*" => "$OUT/all"'
    mkpair a xml '{"anything":"whatever"}'
    run_dispatch
    a_exists "$OUT/all/a.xml"
    cleanup
}

t07() {
    CURRENT="07 no rule -> stays + log"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    mkpair a xml '{"category":"other","group":"g"}'
    run_dispatch
    a_exists "$IN/a.xml"
    a_exists "$IN/a.json"
    a_log "no rule matched source="
    cleanup
}

t08() {
    CURRENT="08 invalid JSON -> stays, others still processed"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    printf 'DATA' > "$IN/bad.xml"
    printf '{not valid' > "$IN/bad.json"
    mkpair good xml '{"category":"report"}'
    run_dispatch
    a_exists "$IN/bad.xml"
    a_exists "$IN/bad.json"
    a_log "reason='invalid JSON'"
    a_exists "$OUT/r/good.xml"
    cleanup
}

t09() {
    CURRENT="09 json without data -> waits"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    printf '{"category":"report"}' > "$IN/lonely.json"
    run_dispatch
    a_exists "$IN/lonely.json"
    a_log "waiting for data file source="
    a_rc 0
    cleanup
}

t10() {
    CURRENT="10 ambiguous (2 data files same base)"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    printf 'A' > "$IN/dup.xml"
    printf 'B' > "$IN/dup.csv"
    printf '{"category":"report"}' > "$IN/dup.json"
    run_dispatch
    a_exists "$IN/dup.xml"
    a_exists "$IN/dup.csv"
    a_log "ambiguous"
    cleanup
}

t11() {
    CURRENT="11 path-traversal guard ('..' in dest)"; new_sandbox
    write_conf 'DIR = "$group"
$category = "report" => "$OUT/$DIR/x"'
    mkpair a xml '{"category":"report","group":"../../etc"}'
    run_dispatch
    a_exists "$IN/a.xml"
    a_log "unsafe or empty destination"
    cleanup
}

t12() {
    CURRENT="12 various extensions routed the same"; new_sandbox
    write_conf '$category = "data" => "$OUT/d"'
    mkpair a xml '{"category":"data"}'
    mkpair b csv '{"category":"data"}'
    mkpair c txt '{"category":"data"}'
    run_dispatch
    a_exists "$OUT/d/a.xml"
    a_exists "$OUT/d/b.csv"
    a_exists "$OUT/d/c.txt"
    cleanup
}

t13() {
    CURRENT="13 JSON archived in single dir"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    mkpair a xml '{"category":"report"}'
    run_dispatch
    a_exists "$ARCH/a.json"
    cleanup
}

t14() {
    CURRENT="14 destination collision -> suffix, no overwrite"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    mkdir -p "$OUT/r"; printf 'OLD' > "$OUT/r/a.xml"
    mkpair a xml '{"category":"report"}' 'NEW'
    run_dispatch
    a_true '[[ "$(cat "$OUT/r/a.xml")" == OLD ]]' "original preserved"
    a_true '[[ $(ls "$OUT/r" | grep -c "a\\.xml\\.") -ge 1 ]]' "suffixed copy created"
    cleanup
}

t15() {
    CURRENT="15 idempotence (2nd run)"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    mkpair a xml '{"category":"report"}'
    mkpair b xml '{"category":"other"}'
    run_dispatch
    run_dispatch
    a_absent "$IN/a.xml"
    a_exists "$IN/b.xml"
    a_true '[[ $(ls "$OUT/r" | grep -c "^a\\.xml$") -eq 1 ]]' "no duplicate of processed file"
    cleanup
}

t16() {
    CURRENT="16 IN membership"; new_sandbox
    write_conf '$category IN ("invoice", "credit") => "$OUT/bill"'
    mkpair a xml '{"category":"credit"}'
    mkpair b xml '{"category":"debit"}'
    run_dispatch
    a_exists "$OUT/bill/a.xml"
    a_exists "$IN/b.xml"
    cleanup
}

t17() {
    CURRENT="17 wildcard in values"; new_sandbox
    write_conf '$name = "invoice*" => "$OUT/inv"
$name = "*credit" => "$OUT/cred"'
    mkpair a xml '{"name":"invoice_2026"}'
    mkpair b xml '{"name":"pre_credit"}'
    mkpair c xml '{"name":"other"}'
    run_dispatch
    a_exists "$OUT/inv/a.xml"
    a_exists "$OUT/cred/b.xml"
    a_exists "$IN/c.xml"
    cleanup
}

t18() {
    CURRENT="18 OR with AND-before-OR precedence"; new_sandbox
    write_conf '$cat = "order" AND $region = "US" OR $cat = "order" AND $region = "CA" => "$OUT/na"'
    mkpair a xml '{"cat":"order","region":"CA"}'
    mkpair b xml '{"cat":"order","region":"FR"}'
    run_dispatch
    a_exists "$OUT/na/a.xml"
    a_exists "$IN/b.xml"
    cleanup
}

t19() {
    CURRENT="19 security: JSON value not executed"; new_sandbox
    write_conf '$category = "report" => "$OUT/$evil"'
    local evil='$(touch '"$SB"'/pwned)'
    mkpair a xml "{\"category\":\"report\",\"evil\":\"$evil\"}"
    run_dispatch
    a_absent "$SB/pwned"
    cleanup
}

t20() {
    CURRENT="20 security: hostile filename (-rf, spaces)"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    printf 'D' > "$IN/-rf weird.xml"
    printf '{"category":"report"}' > "$IN/-rf weird.json"
    run_dispatch
    a_exists "$OUT/r/-rf weird.xml"
    a_rc 0
    cleanup
}

t21() {
    CURRENT="21 security: symlink data rejected"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    printf 'REAL' > "$SB/realdata"
    ln -s "$SB/realdata" "$IN/link.xml"
    printf '{"category":"report"}' > "$IN/link.json"
    run_dispatch
    a_exists "$IN/link.xml"
    a_log "symlink"
    cleanup
}

t22() {
    CURRENT="22 security: newline value does not inject a log line"; new_sandbox
    write_conf '$category = "nomatch" => "$OUT/r"'
    mkpair a xml '{"category":"evil\ninjected","group":"g"}'
    run_dispatch
    a_log "no rule matched source="
    a_true '! grep -qE "^injected" "$LOGF"' "no forged log line"
    cleanup
}

t23() {
    CURRENT="23 REQUIRED missing field"; new_sandbox
    write_conf 'REQUIRED = $category, $group
$category = "report" => "$OUT/r"'
    mkpair a xml '{"category":"report"}'
    run_dispatch
    a_exists "$IN/a.xml"
    a_log "missing/empty required field(s): group"
    cleanup
}

t24() {
    CURRENT="24 REQUIRED present but empty"; new_sandbox
    write_conf 'REQUIRED = $category, $group
$category = "report" => "$OUT/r"'
    mkpair a xml '{"category":"report","group":""}'
    run_dispatch
    a_exists "$IN/a.xml"
    a_log "missing/empty required field(s): group"
    cleanup
}

t25() {
    CURRENT="25 incomplete pair: data without json"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    printf 'D' > "$IN/orphan.csv"
    run_dispatch
    a_exists "$IN/orphan.csv"
    a_log "waiting for metadata source="
    cleanup
}

t26() {
    CURRENT="26 file still being written -> skipped"; new_sandbox
    STABLE=1 write_conf '$category = "report" => "$OUT/r"'
    printf '{"category":"report"}' > "$IN/w.json"
    printf 'start' > "$IN/w.xml"
    ( local i; for i in $(seq 1 40); do printf 'x' >> "$IN/w.xml"; sleep 0.1; done ) &
    local wpid=$!
    run_dispatch
    kill "$wpid" 2>/dev/null; wait "$wpid" 2>/dev/null
    a_exists "$IN/w.xml"
    a_log "still changing"
    cleanup
}

t27() {
    CURRENT="27 quotes: spaces and commas"; new_sandbox
    write_conf '$status IN ("in progress", "on hold") => "$OUT/pending files"
$title = "a,b" => "$OUT/comma"'
    mkpair a xml '{"status":"on hold","title":"z"}'
    mkpair b xml '{"status":"none","title":"a,b"}'
    run_dispatch
    a_exists "$OUT/pending files/a.xml"
    a_exists "$OUT/comma/b.xml"
    cleanup
}

t28() {
    CURRENT="28 segment concatenation"; new_sandbox
    write_conf 'RET = $category"/sub"
$category = "report" => "$OUT/$RET"'
    mkpair a xml '{"category":"report"}'
    run_dispatch
    a_exists "$OUT/report/sub/a.xml"
    cleanup
}

t29() {
    CURRENT="29 preflight: invalid config -> abort, nothing processed"; new_sandbox
    {
        printf 'INCOMING_DIR = "%s"\n' "$IN"
        printf 'JSON_ARCHIVE_DIR = "%s"\n' "$ARCH"
        printf 'LOG_DIR = "%s"\n' "$LOGDIR"
        printf '$category = "report" =>\n'
        printf 'this is not a valid line\n'
    } > "$CONF"
    mkpair a xml '{"category":"report"}'
    run_dispatch
    a_rc 2
    a_exists "$IN/a.xml"
    a_true 'no_files_under "$OUT"' "nothing processed"
    a_log_re "line [0-9]+"
    cleanup
}

t30() {
    CURRENT="30 --check validates without processing"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    mkpair a xml '{"category":"report"}'
    "$DISPATCH" --check "$CONF" >/dev/null 2>&1; local rc_ok=$?
    a_true "[[ $rc_ok -eq 0 ]]" "valid config: --check exit 0"
    a_exists "$IN/a.xml"
    # invalid config -> non-zero
    printf 'this is broken\n' > "$SB/bad.conf"
    "$DISPATCH" --check "$SB/bad.conf" >/dev/null 2>&1; local rc_bad=$?
    a_true "[[ $rc_bad -ne 0 ]]" "invalid config: --check non-zero"
    cleanup
}

t31() {
    CURRENT="31 dry-run: logs actions, moves nothing"; new_sandbox
    write_conf '$category = "report" => "$OUT/reports"'
    mkpair a xml '{"category":"report"}'
    run_dispatch_dry
    a_rc 0
    a_exists "$IN/a.xml"
    a_exists "$IN/a.json"
    a_true 'no_files_under "$OUT"' "nothing moved to destinations"
    a_log "DRY-RUN mode: no files will be moved"
    a_log "DRY-RUN would move source="
    cleanup
}

t32() {
    CURRENT="32 logs split: dispatch.log (all) vs errors.log (WARN/ERROR)"; new_sandbox
    write_conf 'REQUIRED = $category
$category = "report" => "$OUT/r"'
    mkpair good xml '{"category":"report"}'
    mkpair bad  xml '{"other":"x"}'
    run_dispatch
    a_exists "$LOGF"
    a_exists "$ERRF"
    a_true 'grep -qF "missing/empty required field" "$ERRF"' "errors.log has the error"
    a_true '! grep -qF "SUCCESS" "$ERRF"'                     "errors.log excludes successes (INFO)"
    a_true 'grep -qF "SUCCESS" "$LOGF"'                       "dispatch.log has the success"
    a_true 'grep -qF "missing/empty required field" "$LOGF"'  "dispatch.log has the error too"
    cleanup
}

t33() {
    CURRENT="33 --config-file flag (space and = forms)"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    mkpair a xml '{"category":"report"}'
    "$DISPATCH" --config-file "$CONF" --check >/dev/null 2>&1; local r1=$?
    a_true "[[ $r1 -eq 0 ]]" "--config-file FILE honored (--check)"
    "$DISPATCH" "--config-file=$CONF" --check >/dev/null 2>&1; local r2=$?
    a_true "[[ $r2 -eq 0 ]]" "--config-file=FILE honored (--check)"
    "$DISPATCH" --config-file "$CONF" >/dev/null 2>"$SB/stderr"
    a_exists "$OUT/r/a.xml"
    cleanup
}

t34() {
    CURRENT="34 DISPATCH_CONFIG env var + flag precedence"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    mkpair a xml '{"category":"report"}'
    DISPATCH_CONFIG="$CONF" "$DISPATCH" >/dev/null 2>"$SB/stderr"
    a_exists "$OUT/r/a.xml"
    cleanup
    # --config-file overrides $DISPATCH_CONFIG (env points at a broken file)
    new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    mkpair b xml '{"category":"report"}'
    printf 'this is broken\n' > "$SB/bad.conf"
    DISPATCH_CONFIG="$SB/bad.conf" "$DISPATCH" --config-file "$CONF" >/dev/null 2>"$SB/stderr"; local rc=$?
    a_true "[[ $rc -eq 0 ]]" "--config-file overrides \$DISPATCH_CONFIG"
    a_exists "$OUT/r/b.xml"
    cleanup
}

t35() {
    CURRENT="35 flock: a second concurrent run exits without processing"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    mkpair a xml '{"category":"report"}'
    local lock="$LOGDIR/.dispatch.lock"
    : > "$lock"
    exec 8>"$lock"; flock -n 8            # hold the lock like a running instance
    "$DISPATCH" "$CONF" >/dev/null 2>"$SB/stderr"; local rc=$?
    flock -u 8; exec 8>&-                 # release
    a_true "[[ $rc -eq 0 ]]" "second run exits 0"
    a_exists "$IN/a.xml"
    a_log "another instance is already running"
    cleanup
}

t36() {
    CURRENT="36 preflight: non-numeric STABLE_SECONDS rejected"; new_sandbox
    {
        printf 'INCOMING_DIR = "%s"\n' "$IN"
        printf 'JSON_ARCHIVE_DIR = "%s"\n' "$ARCH"
        printf 'LOG_DIR = "%s"\n' "$LOGDIR"
        printf 'STABLE_SECONDS = abc\n'
        printf '$category = "report" => "%s/r"\n' "$OUT"
    } > "$CONF"
    mkpair a xml '{"category":"report"}'
    run_dispatch
    a_rc 2
    a_exists "$IN/a.xml"
    a_log "STABLE_SECONDS"
    cleanup
}

t37() {
    CURRENT="37 --help prints usage and exits 0"; new_sandbox
    "$DISPATCH" --help > "$SB/help.txt" 2>&1; local rc=$?
    a_true "[[ $rc -eq 0 ]]" "--help exit 0"
    a_true 'grep -q -- "--config-file" "$SB/help.txt"' "help mentions --config-file"
    a_true 'grep -q -- "DISPATCH_CONFIG" "$SB/help.txt"' "help mentions DISPATCH_CONFIG"
    cleanup
}

t38() {
    CURRENT="38 mega-complex condition: AND + OR + IN + wildcard + quoted"; new_sandbox
    # (type~invoice* AND region in {EU,UK} AND status=new) OR (priority=high AND flag in {urgent,"on hold"})
    write_conf '$type = "invoice*" AND $region IN ("EU", "UK") AND $status = "new" OR $priority = "high" AND $flag IN ("urgent", "on hold") => "$OUT/complex"'
    mkpair a xml '{"type":"invoice_2026","region":"EU","status":"new"}'   # group 1 true
    mkpair b xml '{"type":"invoice_x","region":"US","status":"new"}'      # region not in list
    mkpair c xml '{"priority":"high","flag":"on hold"}'                   # group 2 true (quoted IN item)
    mkpair d xml '{"priority":"high","flag":"low"}'                       # flag not in list
    mkpair e xml '{"type":"creditnote","region":"EU","status":"new"}'    # type not invoice*
    run_dispatch
    a_exists "$OUT/complex/a.xml"
    a_exists "$OUT/complex/c.xml"
    a_exists "$IN/b.xml"
    a_exists "$IN/d.xml"
    a_exists "$IN/e.xml"
    cleanup
}

t39() {
    CURRENT="39 three OR-groups with mixed operators, correct group matches"; new_sandbox
    write_conf '$a = "x" AND $b = "y" OR $c IN ("p", "q", "r") AND $d = "z*" OR $e = "solo" => "$OUT/multi"'
    mkpair j xml '{"c":"q","d":"zebra"}'     # group 2 true
    mkpair k xml '{"e":"solo"}'              # group 3 true
    mkpair l xml '{"a":"x","b":"no"}'        # group 1 false, others absent
    run_dispatch
    a_exists "$OUT/multi/j.xml"
    a_exists "$OUT/multi/k.xml"
    a_exists "$IN/l.xml"
    cleanup
}

t40() {
    CURRENT="40 log line carries source, exact dest, and SUCCESS status"; new_sandbox
    write_conf '$category = "report" => "$OUT/reports"'
    mkpair a xml '{"category":"report"}'
    run_dispatch
    a_log "SUCCESS source="
    a_log "source='$IN/a.xml'"
    a_log "dest='$OUT/reports'"
    a_log "target='$OUT/reports/a.xml'"
    cleanup
}

t41() {
    CURRENT="41 FAILURE status when destination cannot be created"; new_sandbox
    write_conf '$category = "report" => "$OUT/blocked/sub"'
    printf 'X' > "$OUT/blocked"       # a file where a directory is needed
    mkpair a xml '{"category":"report"}'
    run_dispatch
    a_exists "$IN/a.xml"
    a_log "FAILURE source="
    a_log "cannot create destination"
    cleanup
}

t42() {
    CURRENT="42 --debug traces fields, variables, and rule resolution"; new_sandbox
    write_conf 'GROUP = "$group"
$category = "report" => "$OUT/$GROUP/reports"'
    mkpair a xml '{"category":"report","group":"B"}'
    "$DISPATCH" --debug "$CONF" >/dev/null 2>"$SB/dbg.txt"
    a_log "[DEBUG]"
    a_log "json field: \$category = 'report'"
    a_log "variable: GROUP = 'B'"
    a_log "MATCH; destination resolves to"
    a_exists "$OUT/B/reports/a.xml"
    cleanup
}

t43() {
    CURRENT="43 PYTHON setting selects the interpreter"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    printf 'PYTHON = "%s"\n' "$(command -v python3)" >> "$CONF"
    mkpair a xml '{"category":"report"}'
    run_dispatch
    a_rc 0
    a_exists "$OUT/r/a.xml"
    cleanup
}

t44() {
    CURRENT="44 bad PYTHON path -> exit 3, nothing processed"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    printf 'PYTHON = "/nonexistent/python-xyz"\n' >> "$CONF"
    mkpair a xml '{"category":"report"}'
    run_dispatch
    a_rc 3
    a_exists "$IN/a.xml"
    cleanup
}

# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
main() {
    if ! command -v python3 >/dev/null 2>&1; then
        echo "python3 is required to run the tests" >&2; exit 3
    fi

    # Python unit tests for the engine's parsing / matching core.
    printf '%sengine unit tests (python)%s\n' "$BOLD" "$RESET"
    if python3 "$HERE/test_engine.py"; then ok "engine unit tests passed"; else ko "engine unit tests failed"; fi

    # End-to-end tests.
    local tests
    tests=$(declare -F | awk '{print $3}' | grep -E '^t[0-9]{2}$' | sort)
    local t
    for t in $tests; do
        printf '%s%s%s\n' "$BOLD" "$t" "$RESET"
        "$t"
    done
    echo
    printf '%s================  %d passed, %d failed  ================%s\n' "$BOLD" "$PASS" "$FAIL" "$RESET"
    if (( FAIL > 0 )); then
        printf '%sFailures:%s\n' "$RED" "$RESET"
        local f
        for f in "${FAILURES[@]}"; do printf '  - %s\n' "$f"; done
        exit 1
    fi
    exit 0
}

main "$@"
