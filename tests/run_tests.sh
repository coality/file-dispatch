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
SB="" IN="" ARCH="" OUT="" LOGDIR="" LOGF="" CONF="" RC=0
STABLE=0

RED=$'\033[31m'; GREEN=$'\033[32m'; BOLD=$'\033[1m'; RESET=$'\033[0m'

# --------------------------------------------------------------------------- #
# Harness helpers
# --------------------------------------------------------------------------- #
new_sandbox() {
    SB=$(mktemp -d)
    IN="$SB/incoming"; ARCH="$SB/archive"; OUT="$SB/out"
    LOGDIR="$SB/log"; LOGF="$LOGDIR/dispatch.log"; CONF="$SB/dispatch.conf"
    mkdir -p "$IN" "$OUT" "$LOGDIR"
}

cleanup() { [[ -n $SB && -d $SB ]] && rm -rf "$SB"; SB=""; }

# write a config: base settings + the given variables/rules block
write_conf() {
    {
        printf 'INCOMING_DIR = "%s"\n' "$IN"
        printf 'JSON_ARCHIVE_DIR = "%s"\n' "$ARCH"
        printf 'LOG_FILE = "%s"\n' "$LOGF"
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
    a_log "moved 'example.xml' ->"
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
    a_log "no rule matched for 'a.json'"
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
    a_log "invalid JSON: 'bad.json'"
    a_exists "$OUT/r/good.xml"
    cleanup
}

t09() {
    CURRENT="09 json without data -> waits"; new_sandbox
    write_conf '$category = "report" => "$OUT/r"'
    printf '{"category":"report"}' > "$IN/lonely.json"
    run_dispatch
    a_exists "$IN/lonely.json"
    a_log "waiting for data file of 'lonely.json'"
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
    a_log "no rule matched for 'a.json'"
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
    a_log "waiting for metadata (.json) of 'orphan.csv'"
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
        printf 'LOG_FILE = "%s"\n' "$LOGF"
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

# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
main() {
    if ! command -v jq >/dev/null 2>&1; then
        echo "jq is required to run the tests" >&2; exit 3
    fi
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
