#!/usr/bin/env bash
#
# file-dispatch - route incoming data files to destination directories based on
# the metadata carried by their JSON sidecar, driven by a single config file.
#
# Usage:
#   dispatch.sh [--config-file FILE] [--dry-run] [--debug] [--check]
#     --config-file FILE  path to the config file
#     --dry-run, -n       log what would happen, but move nothing
#     --debug, -d         verbose trace: field values, variables, rule resolution
#     --check             validate the config and exit (0 = OK)
#     --help, -h          show help
#   Config resolution: --config-file  >  $DISPATCH_CONFIG  >  dispatch.conf next to script
#
# Dependencies: bash >= 4, jq, flock (util-linux). All present on target.
#
# Design notes:
#   - The config is parsed, never sourced/eval'd: no code from the config or the
#     JSON is ever executed. JSON field values are pure data.
#   - `set -e` is intentionally NOT used: the script relies on many boolean helper
#     functions and does explicit error handling on every filesystem operation, so
#     it can never exit half-way through a move (which would leave a partial state).

set -uo pipefail
shopt -s nullglob

# --------------------------------------------------------------------------- #
# Globals
# --------------------------------------------------------------------------- #
_src=${BASH_SOURCE[0]}
_dir=${_src%/*}
[[ $_dir == "$_src" ]] && _dir="."
SCRIPT_DIR="$(cd -- "$_dir" && pwd)"
unset _src _dir

# Settings (filled by the config)
INCOMING_DIR=""
JSON_ARCHIVE_DIR=""
LOG_DIR=""
STABLE_SECONDS="2"
declare -a REQUIRED_FIELDS=()

# Derived from LOG_DIR: the two log files
LOG_FILE=""     # $LOG_DIR/dispatch.log  (all actions)
ERROR_LOG=""    # $LOG_DIR/errors.log    (WARN + ERROR only)

# Parsed variables (ordered) and rules (ordered)
declare -a VAR_NAMES=() VAR_EXPRS=()
declare -a RULE_LINE=() RULE_TEXT=() RULE_COND=() RULE_DEST=()

# Per-file context: JSON fields + evaluated variables
declare -A CTX=()
declare -a JSON_KEYS=()

# Config diagnostics
declare -a CONFIG_ERRORS=()
declare -a CONFIG_WARNINGS=()
declare -A SEEN_SETTING=()

# Scratch outputs for helpers that "return" arrays
declare -a SPLIT_RESULT=()
declare -a ATOM_ITEMS=()
ATOM_FIELD=""; ATOM_OP=""; ATOM_RHS=""
EQ_NAME=""; EQ_VAL=""

# Run counters
PROCESSED=0; UNMATCHED=0; INVALID=0; INCOMPLETE=0; UNSTABLE=0; ERRORS=0

CHECK_ONLY=0
DRY_RUN=0
DEBUG=0
CONFIG_PATH=""

# --------------------------------------------------------------------------- #
# Small string helpers
# --------------------------------------------------------------------------- #

# trim leading/trailing whitespace
trim() {
    local s=$1
    s=${s#"${s%%[![:space:]]*}"}
    s=${s%"${s##*[![:space:]]}"}
    printf '%s' "$s"
}

# Split $1 on the literal separator $2, ignoring separators inside '...'/"..."
# quotes. Result goes into the global array SPLIT_RESULT.
split_quote_aware() {
    local s=$1 sep=$2
    local seplen=${#sep}
    SPLIT_RESULT=()
    local buf="" i=0 n=${#s} q=""
    while (( i < n )); do
        local c=${s:i:1}
        if [[ -n $q ]]; then
            buf+=$c
            [[ $c == "$q" ]] && q=""
            i=$((i+1)); continue
        fi
        if [[ $c == '"' || $c == "'" ]]; then
            q=$c; buf+=$c; i=$((i+1)); continue
        fi
        if [[ $seplen -gt 0 && ${s:i:seplen} == "$sep" ]]; then
            SPLIT_RESULT+=("$buf"); buf=""; i=$((i+seplen)); continue
        fi
        buf+=$c; i=$((i+1))
    done
    SPLIT_RESULT+=("$buf")
}

# true if every quote in $1 is closed
quotes_balanced() {
    local s=$1
    local i=0 n=${#s} q=""
    while (( i < n )); do
        local c=${s:i:1}
        if [[ -n $q ]]; then
            [[ $c == "$q" ]] && q=""
        elif [[ $c == '"' || $c == "'" ]]; then
            q=$c
        fi
        i=$((i+1))
    done
    [[ -z $q ]]
}

# Drop an inline "# comment": a top-level '#' preceded by whitespace (or at the
# start of the line) that is not inside quotes.
strip_inline_comment() {
    local s=$1
    local out="" i=0 n=${#s} q="" prev=" "
    while (( i < n )); do
        local c=${s:i:1}
        if [[ -n $q ]]; then
            out+=$c; [[ $c == "$q" ]] && q=""; prev=$c; i=$((i+1)); continue
        fi
        if [[ $c == '"' || $c == "'" ]]; then
            q=$c; out+=$c; prev=$c; i=$((i+1)); continue
        fi
        if [[ $c == '#' && ( $prev == ' ' || $prev == $'\t' ) ]]; then
            break
        fi
        out+=$c; prev=$c; i=$((i+1))
    done
    printf '%s' "$out"
}

# Strip exactly one pair of surrounding quotes (used for plain settings).
unquote_simple() {
    local s=$1
    if (( ${#s} >= 2 )); then
        if [[ $s == '"'*'"' || $s == "'"*"'" ]]; then
            s=${s:1:${#s}-2}
        fi
    fi
    printf '%s' "$s"
}

# Replace $name / ${name} in $1 with CTX[name] (empty if unset). No eval; the
# substituted values are appended as-is and never re-scanned.
expand_refs() {
    local s=$1
    local out="" i=0 n=${#s}
    while (( i < n )); do
        local c=${s:i:1}
        if [[ $c == '$' ]]; then
            local rest=${s:i+1}
            if [[ $rest == '{'* ]]; then
                local inner=${rest:1}
                if [[ $inner == *'}'* ]]; then
                    local name=${inner%%\}*}
                    if [[ $name =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
                        out+=${CTX[$name]-}
                        i=$(( i + 2 + ${#name} + 1 )); continue
                    fi
                fi
                out+='$'; i=$((i+1)); continue
            fi
            local name="" j=0 m=${#rest}
            while (( j < m )); do
                local rc=${rest:j:1}
                [[ $rc == [A-Za-z0-9_] ]] || break
                name+=$rc; j=$((j+1))
            done
            if [[ -z $name ]]; then
                out+='$'; i=$((i+1)); continue
            fi
            out+=${CTX[$name]-}
            i=$(( i + 1 + ${#name} )); continue
        fi
        out+=$c; i=$((i+1))
    done
    printf '%s' "$out"
}

# Assemble a value from adjacent segments (shell-like concatenation):
#   bare run  -> $refs expanded
#   "..."     -> $refs expanded
#   '...'     -> literal
# Wildcards (*) are kept literally so they act as globs at match time.
assemble_value() {
    local s=$1
    local out="" i=0 n=${#s}
    while (( i < n )); do
        local c=${s:i:1}
        if [[ $c == '"' ]]; then
            local j=$((i+1)) seg=""
            while (( j < n )) && [[ ${s:j:1} != '"' ]]; do seg+=${s:j:1}; j=$((j+1)); done
            out+=$(expand_refs "$seg")
            i=$((j+1))
        elif [[ $c == "'" ]]; then
            local j=$((i+1)) seg=""
            while (( j < n )) && [[ ${s:j:1} != "'" ]]; do seg+=${s:j:1}; j=$((j+1)); done
            out+=$seg
            i=$((j+1))
        else
            local j=$i seg=""
            while (( j < n )) && [[ ${s:j:1} != '"' && ${s:j:1} != "'" ]]; do seg+=${s:j:1}; j=$((j+1)); done
            out+=$(expand_refs "$seg")
            i=$j
        fi
    done
    printf '%s' "$out"
}

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

# Remove characters that could forge log lines or inject terminal escapes.
sanitize() {
    local s=$1
    s=${s//$'\n'/ }
    s=${s//$'\r'/ }
    s=${s//$'\t'/ }
    s=${s//$'\033'/?}
    s=${s//$'\a'/}
    s=${s//$'\b'/}
    s=${s//$'\f'/}
    s=${s//$'\v'/}
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
# Config parsing
# --------------------------------------------------------------------------- #

is_reserved_setting() {
    case $1 in
        INCOMING_DIR|JSON_ARCHIVE_DIR|LOG_DIR|STABLE_SECONDS|REQUIRED) return 0 ;;
        *) return 1 ;;
    esac
}

# true if the (comment-stripped) line is a rule, i.e. contains a top-level '=>'
is_rule() {
    split_quote_aware "$1" "=>"
    (( ${#SPLIT_RESULT[@]} >= 2 ))
}

# Split on the first top-level '=' -> EQ_NAME / EQ_VAL
split_first_eq() {
    local s=$1
    local i=0 n=${#s} q=""
    EQ_NAME=$s; EQ_VAL=""
    while (( i < n )); do
        local c=${s:i:1}
        if [[ -n $q ]]; then [[ $c == "$q" ]] && q=""; i=$((i+1)); continue; fi
        if [[ $c == '"' || $c == "'" ]]; then q=$c; i=$((i+1)); continue; fi
        if [[ $c == '=' ]]; then
            EQ_NAME=${s:0:i}; EQ_VAL=${s:i+1}; return 0
        fi
        i=$((i+1))
    done
    return 1
}

# Parse one condition atom. Sets ATOM_FIELD / ATOM_OP (EQ|IN) / ATOM_RHS / ATOM_ITEMS.
# Returns 1 if the atom is syntactically invalid.
parse_atom() {
    local atom; atom=$(trim "$1")
    ATOM_FIELD=""; ATOM_OP=""; ATOM_RHS=""; ATOM_ITEMS=()
    local rest name
    if [[ $atom == '${'* ]]; then
        rest=${atom#'${'}
        [[ $rest == *'}'* ]] || return 1
        name=${rest%%\}*}
        [[ $name =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
        rest=${rest#*\}}
    elif [[ $atom == '$'* ]]; then
        rest=${atom#\$}
        name=""
        local i=0 n=${#rest}
        while (( i < n )); do
            local c=${rest:i:1}
            [[ $c == [A-Za-z0-9_] ]] || break
            name+=$c; i=$((i+1))
        done
        [[ -n $name ]] || return 1
        rest=${rest:${#name}}
    else
        return 1
    fi
    ATOM_FIELD=$name
    rest=$(trim "$rest")
    if [[ $rest == IN[[:space:]]* || $rest == 'IN('* ]]; then
        rest=${rest#IN}
        rest=$(trim "$rest")
        [[ $rest == '('* && $rest == *')' ]] || return 1
        local inner=${rest#\(}; inner=${inner%\)}
        ATOM_OP="IN"
        split_quote_aware "$inner" ","
        local it items=()
        for it in "${SPLIT_RESULT[@]}"; do
            it=$(trim "$it")
            [[ -z $it ]] && return 1
            items+=("$it")
        done
        (( ${#items[@]} >= 1 )) || return 1
        ATOM_ITEMS=("${items[@]}")
        return 0
    fi
    if [[ $rest == '='* ]]; then
        ATOM_OP="EQ"
        ATOM_RHS=$(trim "${rest#=}")
        [[ -n $ATOM_RHS ]] || return 1
        return 0
    fi
    return 1
}

validate_cond_structure() {
    local cond=$1 lineno=$2
    if ! quotes_balanced "$cond"; then
        CONFIG_ERRORS+=("line $lineno: unbalanced quotes in condition"); return
    fi
    if [[ $cond =~ ^(AND|OR)([[:space:]]|$) || $cond =~ ([[:space:]]|^)(AND|OR)$ ]]; then
        CONFIG_ERRORS+=("line $lineno: dangling AND/OR in condition"); return
    fi
    split_quote_aware "$cond" " OR "
    local groups=("${SPLIT_RESULT[@]}")
    local g
    for g in "${groups[@]}"; do
        split_quote_aware "$g" " AND "
        local atoms=("${SPLIT_RESULT[@]}")
        local a
        for a in "${atoms[@]}"; do
            a=$(trim "$a")
            if [[ -z $a ]]; then
                CONFIG_ERRORS+=("line $lineno: empty condition (dangling AND/OR?)"); continue
            fi
            if ! parse_atom "$a"; then
                CONFIG_ERRORS+=("line $lineno: invalid condition '$a'")
            fi
        done
    done
}

parse_rule() {
    local body=$1 lineno=$2 text=$3
    split_quote_aware "$body" "=>"
    if (( ${#SPLIT_RESULT[@]} != 2 )); then
        CONFIG_ERRORS+=("line $lineno: a rule must contain exactly one '=>'"); return
    fi
    local cond dest
    cond=$(trim "${SPLIT_RESULT[0]}")
    dest=$(trim "${SPLIT_RESULT[1]}")
    [[ -z $cond ]] && CONFIG_ERRORS+=("line $lineno: rule has an empty condition")
    [[ -z $dest ]] && CONFIG_ERRORS+=("line $lineno: rule has an empty destination")
    if ! quotes_balanced "$dest"; then
        CONFIG_ERRORS+=("line $lineno: unbalanced quotes in destination")
    fi
    [[ -n $cond ]] && validate_cond_structure "$cond" "$lineno"
    RULE_LINE+=("$lineno"); RULE_TEXT+=("$text"); RULE_COND+=("$cond"); RULE_DEST+=("$dest")
}

parse_required() {
    local val=$1 lineno=$2
    split_quote_aware "$val" ","
    local items=("${SPLIT_RESULT[@]}")
    local it
    for it in "${items[@]}"; do
        it=$(trim "$it")
        [[ -z $it ]] && continue
        local name=$it
        if [[ $name == '${'*'}' ]]; then
            name=${name#'${'}; name=${name%'}'}
        elif [[ $name == '$'* ]]; then
            name=${name#\$}
        fi
        if [[ $name =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
            REQUIRED_FIELDS+=("$name")
        else
            CONFIG_ERRORS+=("line $lineno: invalid field in REQUIRED: '$it'")
        fi
    done
}

assign_setting() {
    local name=$1 val=$2 lineno=$3
    if [[ -n ${SEEN_SETTING[$name]:-} ]]; then
        CONFIG_WARNINGS+=("line $lineno: duplicate setting '$name' (last one wins)")
    fi
    SEEN_SETTING[$name]=1
    if [[ $name == REQUIRED ]]; then
        parse_required "$val" "$lineno"; return
    fi
    local uq; uq=$(unquote_simple "$val")
    case $name in
        INCOMING_DIR)     INCOMING_DIR=$uq ;;
        JSON_ARCHIVE_DIR) JSON_ARCHIVE_DIR=$uq ;;
        LOG_DIR)          LOG_DIR=$uq ;;
        STABLE_SECONDS)   STABLE_SECONDS=$uq ;;
    esac
}

parse_config() {
    local file=$1
    if [[ ! -r $file ]]; then
        CONFIG_ERRORS+=("cannot read config file: $file"); return
    fi
    local lineno=0 raw line
    while IFS= read -r raw || [[ -n $raw ]]; do
        lineno=$((lineno+1))
        line=$(strip_inline_comment "$raw")
        line=$(trim "$line")
        [[ -z $line ]] && continue
        if is_rule "$line"; then
            parse_rule "$line" "$lineno" "$line"
        elif split_first_eq "$line"; then
            local name val
            name=$(trim "$EQ_NAME"); val=$(trim "$EQ_VAL")
            if is_reserved_setting "$name"; then
                assign_setting "$name" "$val" "$lineno"
            elif [[ $name =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
                VAR_NAMES+=("$name"); VAR_EXPRS+=("$val")
            else
                CONFIG_ERRORS+=("line $lineno: invalid variable name '$name'")
            fi
        else
            CONFIG_ERRORS+=("line $lineno: unrecognized line: $line")
        fi
    done < "$file"
}

validate_config_semantics() {
    [[ -n $INCOMING_DIR ]]     || CONFIG_ERRORS+=("missing required setting: INCOMING_DIR")
    [[ -n $JSON_ARCHIVE_DIR ]] || CONFIG_ERRORS+=("missing required setting: JSON_ARCHIVE_DIR")
    [[ -n $LOG_DIR ]]          || CONFIG_ERRORS+=("missing required setting: LOG_DIR")
    [[ $STABLE_SECONDS =~ ^[0-9]+$ ]] || CONFIG_ERRORS+=("STABLE_SECONDS must be a non-negative integer (got '$STABLE_SECONDS')")
    if [[ -n $LOG_DIR ]]; then
        LOG_FILE="$LOG_DIR/dispatch.log"
        ERROR_LOG="$LOG_DIR/errors.log"
    fi
}

report_config_diag() {
    local ts; ts=$(date '+%Y-%m-%dT%H:%M:%S%z')
    local w e
    for w in "${CONFIG_WARNINGS[@]}"; do
        local l="$ts [WARN] config: $(sanitize "$w")"
        printf '%s\n' "$l" >&2
        [[ -n ${LOG_FILE:-} ]] && printf '%s\n' "$l" >>"$LOG_FILE" 2>/dev/null || true
    done
    for e in "${CONFIG_ERRORS[@]}"; do
        local l="$ts [ERROR] config: $(sanitize "$e")"
        printf '%s\n' "$l" >&2
        [[ -n ${LOG_FILE:-} ]]  && printf '%s\n' "$l" >>"$LOG_FILE"  2>/dev/null || true
        [[ -n ${ERROR_LOG:-} ]] && printf '%s\n' "$l" >>"$ERROR_LOG" 2>/dev/null || true
    done
}

# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

atom_true() {
    parse_atom "$1" || return 1
    local lval=${CTX[$ATOM_FIELD]-}
    if [[ $ATOM_OP == EQ ]]; then
        local pat; pat=$(assemble_value "$ATOM_RHS")
        if [[ $lval == $pat ]]; then
            (( DEBUG == 1 )) && dbg "      atom \$$ATOM_FIELD = \"$pat\"  ('$lval')  -> true"
            return 0
        fi
        (( DEBUG == 1 )) && dbg "      atom \$$ATOM_FIELD = \"$pat\"  ('$lval')  -> false"
        return 1
    fi
    local it pat
    for it in "${ATOM_ITEMS[@]}"; do
        pat=$(assemble_value "$it")
        if [[ $lval == $pat ]]; then
            (( DEBUG == 1 )) && dbg "      atom \$$ATOM_FIELD IN (...)  ('$lval' == \"$pat\")  -> true"
            return 0
        fi
    done
    (( DEBUG == 1 )) && dbg "      atom \$$ATOM_FIELD IN (...)  ('$lval' in none)  -> false"
    return 1
}

# true if the condition (DNF: OR of AND-groups) matches the current CTX
rule_matches() {
    local cond=$1
    split_quote_aware "$cond" " OR "
    local groups=("${SPLIT_RESULT[@]}")
    local g
    for g in "${groups[@]}"; do
        split_quote_aware "$g" " AND "
        local atoms=("${SPLIT_RESULT[@]}")
        local all=1 a
        for a in "${atoms[@]}"; do
            if ! atom_true "$a"; then all=0; break; fi
        done
        [[ $all == 1 ]] && return 0
    done
    return 1
}

# --------------------------------------------------------------------------- #
# Processing
# --------------------------------------------------------------------------- #

load_ctx() {
    local jf=$1
    CTX=()
    JSON_KEYS=()
    local key
    while IFS= read -r key; do
        [[ -z $key ]] && continue
        local val
        val=$(jq -r --arg k "$key" '.[$k] | if . == null then "" else tostring end' "$jf" 2>/dev/null)
        val=${val//$'\n'/ }
        val=${val//$'\r'/ }
        CTX[$key]=$val
        JSON_KEYS+=("$key")
    done < <(jq -r 'keys_unsorted[]' "$jf" 2>/dev/null)
}

ctx_summary() {
    local keys=() k out="" first=1 count=0
    if (( ${#REQUIRED_FIELDS[@]} > 0 )); then
        keys=("${REQUIRED_FIELDS[@]}")
    else
        keys=("${JSON_KEYS[@]}")
    fi
    for k in "${keys[@]}"; do
        if (( count >= 6 )); then out+=", ..."; break; fi
        [[ $first == 1 ]] && first=0 || out+=", "
        out+="$k=${CTX[$k]:-}"
        count=$((count+1))
    done
    printf 'fields: %s' "$out"
}

# echo a non-colliding path for "$dir/$name" (adds a timestamp suffix on clash)
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
    if ! jq -e . "$jf" >/dev/null 2>&1; then
        log ERROR "FAILURE source='$jf' reason='invalid JSON' - left in place"; INVALID=$((INVALID+1)); return
    fi

    load_ctx "$jf"

    if (( DEBUG == 1 )); then
        dbg "processing pair: source='$df' meta='$jf'"
        local dk
        for dk in "${JSON_KEYS[@]}"; do dbg "  json field: \$$dk = '${CTX[$dk]}'"; done
    fi

    local missing=() f
    for f in "${REQUIRED_FIELDS[@]}"; do
        [[ -z ${CTX[$f]:-} ]] && missing+=("$f")
    done
    if (( ${#missing[@]} > 0 )); then
        log ERROR "FAILURE source='$jf' reason='missing/empty required field(s): ${missing[*]}' - left in place"
        ERRORS=$((ERRORS+1)); return
    fi

    # Evaluate user variables in order (into CTX)
    local i
    for i in "${!VAR_NAMES[@]}"; do
        CTX[${VAR_NAMES[$i]}]=$(assemble_value "${VAR_EXPRS[$i]}")
        (( DEBUG == 1 )) && dbg "  variable: ${VAR_NAMES[$i]} = '${CTX[${VAR_NAMES[$i]}]}'"
    done

    # First matching rule wins
    local matched=0 dest="" ruletext="" ruleno=""
    for i in "${!RULE_COND[@]}"; do
        (( DEBUG == 1 )) && dbg "  rule #${RULE_LINE[$i]}: ${RULE_COND[$i]} => ${RULE_DEST[$i]}"
        if rule_matches "${RULE_COND[$i]}"; then
            dest=$(assemble_value "${RULE_DEST[$i]}")
            ruletext=${RULE_TEXT[$i]}
            ruleno=${RULE_LINE[$i]}
            matched=1
            (( DEBUG == 1 )) && dbg "  -> MATCH; destination resolves to '$dest'"
            break
        fi
        (( DEBUG == 1 )) && dbg "  -> no match"
    done

    if (( matched == 0 )); then
        log WARN "no rule matched source='$jf' ($(ctx_summary)) - left in place"
        UNMATCHED=$((UNMATCHED+1)); return
    fi

    if [[ -z $dest || $dest == *..* ]]; then
        log ERROR "FAILURE source='$df' dest='$dest' reason='unsafe or empty destination (contains ..)' - left in place"
        ERRORS=$((ERRORS+1)); return
    fi

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
        '  1. --config-file FILE' \
        '  2. $DISPATCH_CONFIG environment variable' \
        '  3. dispatch.conf next to the script'
}

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
    # Config resolution: --config-file / positional  >  $DISPATCH_CONFIG  >  default
    [[ -z $CONFIG_PATH ]] && CONFIG_PATH=${DISPATCH_CONFIG:-}
    [[ -z $CONFIG_PATH ]] && CONFIG_PATH="$SCRIPT_DIR/dispatch.conf"

    if ! command -v jq >/dev/null 2>&1; then
        echo "$(date '+%Y-%m-%dT%H:%M:%S%z') [ERROR] required dependency 'jq' not found" >&2
        exit 3
    fi

    # Preflight: parse + validate config before doing anything else.
    parse_config "$CONFIG_PATH"
    validate_config_semantics
    if (( ${#CONFIG_ERRORS[@]} > 0 )); then
        [[ -n $LOG_DIR ]] && mkdir -p -- "$LOG_DIR" 2>/dev/null || true
        report_config_diag
        exit 2
    fi
    report_config_diag  # warnings only, if any

    if (( CHECK_ONLY == 1 )); then
        echo "config OK: $CONFIG_PATH" >&2
        exit 0
    fi

    # The logs directory holds dispatch.log and errors.log; always created so we can log.
    mkdir -p -- "$LOG_DIR" 2>/dev/null || true

    if (( DRY_RUN == 1 )); then
        log INFO "mode: no files will be moved"
    else
        mkdir -p -- "$JSON_ARCHIVE_DIR" 2>/dev/null || true
        # Prevent overlapping cron runs (lock kept in the admin-controlled logs dir).
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
