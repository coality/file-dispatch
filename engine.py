#!/usr/bin/env python3
"""
file-dispatch engine: the parsing / matching core (a pure library).

dispatch.py (the orchestrator) imports this module. Everything that is fiddly --
parsing the config DSL, the rule grammar (AND / OR / IN / wildcards / quotes /
concatenation), variable expansion and matching a JSON file against the rules --
lives here, where it is easy to read and unit-test (see tests/test_engine.py).

Main entry points:
  Config().parse(path); Config().validate()   -> fills .settings/.vars/.rules,
                                                  .errors, .warnings
  Config().resolve(jsonfile, debug)            -> {status, dest, ruleno, ...}

How a file gets routed, end to end:

  dispatch.conf  --parse-->  settings + vars + rules      (once, at startup)
                                        |
  <base>.json    --json.load-->  ctx (one entry per field)
                                        |
                             vars evaluated top to bottom,
                             each one added to the same ctx
                                        |
                      rules tried in file order, first match wins
                                        |
                        destination = the rule's value expression
                                    assembled against ctx

So a rule condition and a variable both see exactly the same namespace: JSON
fields first, then whatever variables were defined above the line being read.
That single ctx is why "$OUT/$group" works the same in a rule as in a variable.

Three small languages live here, in this order:
  1. values       "text" $field func(...) a + b   -> assemble_value()
  2. conditions   $field OP value, AND/OR/( )     -> parse_condition() + AST
  3. the file     settings / variables / rules    -> Config.parse()

Standard library only.
"""

import fnmatch
import json
import re

RESERVED = {
    "INCOMING_DIR", "JSON_ARCHIVE_DIR", "LOG_DIR",
    "STABLE_SECONDS", "REQUIRED", "PYTHON", "CREATE_DIRS",
    "DISPATCH_WITHOUT_JSON", "DRY_RUN", "DEBUG",
    "LOG_MAX_MB", "LOG_KEEP", "REPORT_DIR", "REPORT_KEEP_DAYS", "REPORT_SPLIT",
}

REPORT_SPLITS = ("none", "daily", "monthly")

# The yes/no settings, all validated the same way and all overridable from the
# command line for the two that have a flag (DRY_RUN, DEBUG).
BOOL_SETTINGS = ("CREATE_DIRS", "DISPATCH_WITHOUT_JSON", "DRY_RUN", "DEBUG")

# Fields the filesystem itself provides, present for every file whether or not
# it has a sidecar (dispatch.py fills them in; see system_fields there):
#   $Filename      base name with extension, e.g. "orders-42.csv"
#   $Filesize      size in bytes as digits, so < > <= >= work on it
#   $Filedatetime  mtime as "YYYY-MM-DDTHH:MM:SS", local time
# A sidecar field of the same name wins: the producer's metadata is the
# authority, these only fill in what it does not say.
SYSTEM_FIELDS = ("Filename", "Filesize", "Filedatetime")

# Accepted spellings for the yes/no settings, and what they mean.
BOOLS = {"yes": True, "no": False, "true": True, "false": False, "1": True, "0": False,
         "on": True, "off": False}
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# --------------------------------------------------------------------------- #
# String helpers (mirror the config DSL semantics)
# --------------------------------------------------------------------------- #
def sanitize(s):
    """Strip characters that would break the tab protocol or forge log lines."""
    return s.replace("\n", " ").replace("\r", " ").replace("\t", " ")


def strip_inline_comment(s):
    """Drop a top-level '#' comment preceded by whitespace (outside quotes)."""
    out, q, prev = [], None, " "
    for c in s:
        if q:
            out.append(c)
            if c == q:
                q = None
            prev = c
            continue
        if c in ('"', "'"):
            q = c
            out.append(c)
            prev = c
            continue
        if c == "#" and prev in (" ", "\t"):
            break
        out.append(c)
        prev = c
    return "".join(out)


def split_quote_aware(s, sep):
    """Split s on the literal separator sep, ignoring separators inside quotes."""
    res, buf, q, i, n, sl = [], [], None, 0, len(s), len(sep)
    while i < n:
        c = s[i]
        if q:
            buf.append(c)
            if c == q:
                q = None
            i += 1
            continue
        if c in ('"', "'"):
            q = c
            buf.append(c)
            i += 1
            continue
        if sl and s[i:i + sl] == sep:
            res.append("".join(buf))
            buf = []
            i += sl
            continue
        buf.append(c)
        i += 1
    res.append("".join(buf))
    return res


def quotes_balanced(s):
    q = None
    for c in s:
        if q:
            if c == q:
                q = None
        elif c in ('"', "'"):
            q = c
    return q is None


def unquote_simple(s):
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        return s[1:-1]
    return s


def split_first_eq(s):
    """Return (name, value) split on the first top-level '=', or None."""
    q = None
    for i, c in enumerate(s):
        if q:
            if c == q:
                q = None
        elif c in ('"', "'"):
            q = c
        elif c == "=":
            return s[:i], s[i + 1:]
    return None


def is_rule(s):
    return len(split_quote_aware(s, "=>")) >= 2


def expand_refs(s, ctx):
    """Replace $name / ${name} with ctx[name] (empty if unset). No re-scanning."""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "$":
            rest = s[i + 1:]
            if rest.startswith("{"):
                j = rest.find("}")
                if j != -1:
                    name = rest[1:j]
                    if IDENT_RE.fullmatch(name):
                        out.append(ctx.get(name, ""))
                        i += 1 + (j + 1)
                        continue
                out.append("$")
                i += 1
                continue
            m = IDENT_RE.match(rest)
            if m:
                name = m.group(0)
                out.append(ctx.get(name, ""))
                i += 1 + len(name)
                continue
            out.append("$")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------- #
# The value language
#
# A value is a run of segments glued together, left to right:
#     "quoted"   $refs expanded inside it
#     'quoted'   taken literally, $ included
#     func(...)  int() / upper() / lower(), applied to the value inside
#     bare       $refs expanded; anything else is literal text
# Segments may simply touch ($a"/"$b) or be joined with + ($a + "/" + $b).
# assemble_value() below is the single place where all of this happens, which
# is why one change there covers variables, ternary branches, rule
# destinations, the right-hand side of a condition and IN list items alike.
# --------------------------------------------------------------------------- #
def _cast_int(v):
    """Cast a numeric string to its integer form; leave non-numbers unchanged."""
    try:
        return str(int(float(v)))
    except (ValueError, TypeError):
        return v


# Value-expression functions: func(<value>). Quote text to use these names literally.
FUNCS = {
    "int": _cast_int,
    "upper": lambda v: v.upper(),
    "lower": lambda v: v.lower(),
}
_FUNC_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _func_at(s, i):
    """If a known FUNCS call starts at s[i], return (name, open_paren, close_paren)."""
    m = _FUNC_RE.match(s, i)
    if not m or m.group(1) not in FUNCS:
        return None
    open_idx = m.end() - 1
    depth, q, j, n = 0, None, open_idx, len(s)
    while j < n:
        ch = s[j]
        if q:
            if ch == q:
                q = None
        elif ch in ('"', "'"):
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return (m.group(1), open_idx, j)
        j += 1
    return None


def assemble_value(s, ctx):
    """Concatenate adjacent segments into a string:
      "..."   -> $refs expanded         '...'   -> literal
      func(…) -> function applied       bare    -> $refs expanded

    Segments may simply sit next to each other ($a"/"$b) or be joined with an
    explicit "+" ($a + "/" + $b), which also absorbs the spaces around it. A "+"
    inside quotes is literal text, not an operator.
    """
    out, i, n, bare = [], 0, len(s), 0
    while i < n:
        c = s[i]
        if c == "+":
            if i > bare:
                out.append(expand_refs(s[bare:i].rstrip(), ctx))
            i += 1
            while i < n and s[i].isspace():
                i += 1
            bare = i
            continue
        if c in ('"', "'"):
            if i > bare:
                out.append(expand_refs(s[bare:i], ctx))
            j = i + 1
            while j < n and s[j] != c:
                j += 1
            out.append(expand_refs(s[i + 1:j], ctx) if c == '"' else s[i + 1:j])
            i = bare = j + 1
            continue
        fn = _func_at(s, i)
        if fn:
            name, open_idx, close_idx = fn
            if i > bare:
                out.append(expand_refs(s[bare:i], ctx))
            out.append(FUNCS[name](assemble_value(s[open_idx + 1:close_idx], ctx)))
            i = bare = close_idx + 1
            continue
        i += 1
    if n > bare:
        out.append(expand_refs(s[bare:n], ctx))
    return "".join(out)


class Fields(dict):
    """Field values, plus the names that arrived as JSON null.

    null has no string form, so it reads as "" like an absent or empty field.
    Keeping the set apart is what lets REQUIRED and ISNULL tell them apart. An
    attribute, not a key: a key could collide with a field of the same name in
    an untrusted JSON.
    """
    nulls = frozenset()


def setting_bool(settings, name, default=False):
    """Read a yes/no setting. Unset or unparseable falls back to 'default';
    validate() is what reports an unparseable one, so this never raises."""
    return BOOLS.get(str(settings.get(name, "")).strip().lower(), default)


def parse_atom(atom):
    """Parse '$field = value', '$field IN (a, b)' or '$field ISNULL'. Return dict or None."""
    atom = atom.strip()
    if atom.startswith("${"):
        rest = atom[2:]
        j = rest.find("}")
        if j == -1:
            return None
        name = rest[:j]
        if not IDENT_RE.fullmatch(name):
            return None
        rest = rest[j + 1:]
    elif atom.startswith("$"):
        m = IDENT_RE.match(atom[1:])
        if not m:
            return None
        name = m.group(0)
        rest = atom[1 + len(name):]
    else:
        return None
    rest = rest.strip()
    if rest in ("ISNULL", "ISNOTNULL"):                      # no right-hand side
        return {"op": rest, "field": name}
    if rest.startswith("IN ") or rest.startswith("IN\t") or rest.startswith("IN("):
        rest = rest[2:].strip()
        if not (rest.startswith("(") and rest.endswith(")")):
            return None
        items = [it.strip() for it in split_quote_aware(rest[1:-1], ",")]
        if not items or any(it == "" for it in items):
            return None
        return {"op": "IN", "field": name, "items": items}
    for kw in ("STARTSWITH", "ENDSWITH", "CONTAINS"):        # string predicates
        if rest.startswith(kw + " ") or rest.startswith(kw + "\t"):
            rhs = rest[len(kw):].strip()
            if rhs == "":
                return None
            return {"op": kw, "field": name, "rhs": rhs}
    if rest.startswith("!=") or rest.startswith("<>"):       # not-equal
        rhs = rest[2:].strip()
        if rhs == "":
            return None
        return {"op": "NE", "field": name, "rhs": rhs}
    for pfx, op in (("<=", "LE"), (">=", "GE"), ("<", "LT"), (">", "GT")):  # numeric
        if rest.startswith(pfx):
            rhs = rest[len(pfx):].strip()
            if rhs == "":
                return None
            return {"op": op, "field": name, "rhs": rhs}
    if rest.startswith("="):
        rest = rest[1:]
        if rest.startswith("="):        # accept Python-style '=='
            rest = rest[1:]
        rhs = rest.strip()
        if rhs == "":
            return None
        return {"op": "EQ", "field": name, "rhs": rhs}
    return None


def to_str(v):
    """Mirror jq 'tostring' closely enough for flat metadata values."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Condition grammar:  expr := or ;  or := and ("OR" and)* ;
#                     and := factor ("AND" factor)* ;  factor := "(" expr ")" | atom
# Grouping "(...)" is distinct from the "(...)" of an IN value list, which is
# consumed as part of an atom by the tokenizer.
# --------------------------------------------------------------------------- #
class ConditionError(Exception):
    pass


def tokenize_condition(cond):
    """Split a condition into tokens: ('LP',) ('RP',) ('AND',) ('OR',) ('ATOM', s)."""
    toks = []
    i, n = 0, len(cond)
    while i < n:
        while i < n and cond[i] in " \t":
            i += 1
        if i >= n:
            break
        c = cond[i]
        if c == "(":
            toks.append(("LP",)); i += 1; continue
        if c == ")":
            toks.append(("RP",)); i += 1; continue
        if cond[i:i + 3] == "AND" and (i + 3 == n or cond[i + 3] in " \t()"):
            toks.append(("AND",)); i += 3; continue
        if cond[i:i + 2] == "OR" and (i + 2 == n or cond[i + 2] in " \t()"):
            toks.append(("OR",)); i += 2; continue
        # An atom: consume up to a top-level boundary (AND / OR / grouping ')').
        # "Top-level" is the whole difficulty here. An atom may itself contain
        # quotes and parentheses -- $x IN ("a", "b") -- and those must not be
        # mistaken for the grammar's own ( ) or for a keyword:
        #   q      != None while inside a quoted string: nothing counts there
        #   depth  > 0 while inside an IN list: its ")" belongs to the atom
        # so only a ")" at depth 0 ends the atom, and only whitespace at
        # depth 0 is worth peeking past for an AND / OR.
        start, depth, q = i, 0, None
        while i < n:
            ch = cond[i]
            if q:
                if ch == q:
                    q = None
                i += 1; continue
            if ch in ('"', "'"):
                q = ch; i += 1; continue
            if ch == "(":
                depth += 1; i += 1; continue
            if ch == ")":
                if depth == 0:
                    break               # closes a grouping, not part of us
                depth -= 1; i += 1; continue
            if depth == 0 and ch in " \t":
                # Whitespace only ends the atom if a keyword follows it:
                # "$a = x AND $b = y" stops here, "$a IN (x, y)" does not.
                j = i
                while j < n and cond[j] in " \t":
                    j += 1
                if cond[j:j + 3] == "AND" and (j + 3 == n or cond[j + 3] in " \t()"):
                    break
                if cond[j:j + 2] == "OR" and (j + 2 == n or cond[j + 2] in " \t()"):
                    break
            i += 1
        toks.append(("ATOM", cond[start:i].strip()))
    return toks


def _tok_str(t):
    return {"LP": "(", "RP": ")", "AND": "AND", "OR": "OR"}.get(t[0], t[1] if len(t) > 1 else t[0])


def parse_condition(cond):
    """Parse a condition into an AST, or raise ConditionError.

    AST: ('AND', a, b) | ('OR', a, b) | ('ATOM', <parsed atom dict>).
    """
    if not quotes_balanced(cond):
        raise ConditionError("unbalanced quotes in condition")
    toks = tokenize_condition(cond)
    if not toks:
        raise ConditionError("empty condition")
    pos = [0]
    node = _parse_or(toks, pos)
    if pos[0] != len(toks):
        raise ConditionError("unexpected '%s' in condition" % _tok_str(toks[pos[0]]))
    return node


# The three functions below are a textbook recursive-descent parser, one per
# precedence level: _parse_or calls _parse_and calls _parse_factor. Because OR
# sits at the outermost level and AND inside it, AND binds tighter -- that is
# where "A OR B AND C" reading as "A OR (B AND C)" comes from, without any
# precedence table. `pos` is a one-element list used as a mutable cursor shared
# by all three (a plain int would be copied, not advanced, for the caller).
def _parse_or(toks, pos):
    node = _parse_and(toks, pos)
    while pos[0] < len(toks) and toks[pos[0]][0] == "OR":
        pos[0] += 1
        node = ("OR", node, _parse_and(toks, pos))
    return node


def _parse_and(toks, pos):
    node = _parse_factor(toks, pos)
    while pos[0] < len(toks) and toks[pos[0]][0] == "AND":
        pos[0] += 1
        node = ("AND", node, _parse_factor(toks, pos))
    return node


def _parse_factor(toks, pos):
    if pos[0] >= len(toks):
        raise ConditionError("condition ends unexpectedly (dangling AND/OR?)")
    t = toks[pos[0]]
    if t[0] == "LP":
        pos[0] += 1
        node = _parse_or(toks, pos)
        if pos[0] >= len(toks) or toks[pos[0]][0] != "RP":
            raise ConditionError("missing ')' in condition")
        pos[0] += 1
        return node
    if t[0] == "ATOM":
        at = parse_atom(t[1])
        if at is None:
            raise ConditionError("invalid condition '%s'" % t[1])
        pos[0] += 1
        return ("ATOM", at)
    raise ConditionError("unexpected '%s' in condition" % _tok_str(t))


OP_SYM = {"EQ": "=", "NE": "!=", "LT": "<", "GT": ">", "LE": "<=", "GE": ">=",
          "STARTSWITH": "STARTSWITH", "ENDSWITH": "ENDSWITH", "CONTAINS": "CONTAINS",
          "ISNULL": "ISNULL", "ISNOTNULL": "ISNOTNULL"}


def _to_num(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def compare_atom(op, lval, pat):
    """Compare a field value to a pattern for a single-value operator.

    EQ/NE use glob matching; STARTSWITH/ENDSWITH/CONTAINS are plain substring
    tests; </>/<=/>= are numeric (non-numeric operands -> False).
    """
    if op == "EQ":
        return fnmatch.fnmatchcase(lval, pat)
    if op == "NE":
        return not fnmatch.fnmatchcase(lval, pat)
    if op == "STARTSWITH":
        return lval.startswith(pat)
    if op == "ENDSWITH":
        return lval.endswith(pat)
    if op == "CONTAINS":
        return pat in lval
    a, b = _to_num(lval), _to_num(pat)
    if a is None or b is None:
        return False
    if op == "LT":
        return a < b
    if op == "GT":
        return a > b
    if op == "LE":
        return a <= b
    if op == "GE":
        return a >= b
    return False


def is_null(at, ctx):
    """Truth of an ISNULL / ISNOTNULL atom."""
    null = at["field"] in getattr(ctx, "nulls", ())
    return null if at["op"] == "ISNULL" else not null


def atom_matches(at, ctx):
    """Evaluate a parsed atom against ctx (used by rules and ternary conditions)."""
    if at["op"] in ("ISNULL", "ISNOTNULL"):
        return is_null(at, ctx)
    lval = ctx.get(at["field"], "")
    if at["op"] == "IN":
        return any(fnmatch.fnmatchcase(lval, assemble_value(it, ctx)) for it in at["items"])
    return compare_atom(at["op"], lval, assemble_value(at["rhs"], ctx))


def eval_condition_ast(node, ctx):
    kind = node[0]
    if kind == "ATOM":
        return atom_matches(node[1], ctx)
    if kind == "AND":
        return eval_condition_ast(node[1], ctx) and eval_condition_ast(node[2], ctx)
    if kind == "OR":
        return eval_condition_ast(node[1], ctx) or eval_condition_ast(node[2], ctx)
    return False


# --------------------------------------------------------------------------- #
# Variable value expression: a plain value, or a Python-style ternary
#   <value> if <condition> else <value>      (nestable, like if/elif/else)
# --------------------------------------------------------------------------- #
def find_top_level(s, sep):
    """Index of the first `sep` at paren-depth 0 and outside quotes, or None."""
    i, n, q, depth, sl = 0, len(s), None, 0, len(sep)
    while i < n:
        c = s[i]
        if q:
            if c == q:
                q = None
            i += 1; continue
        if c in ('"', "'"):
            q = c; i += 1; continue
        if c == "(":
            depth += 1; i += 1; continue
        if c == ")":
            if depth > 0:
                depth -= 1
            i += 1; continue
        if depth == 0 and s[i:i + sl] == sep:
            return i
        i += 1
    return None


def parse_vexpr(expr):
    """Parse a variable value expression. Returns:
         ('val', raw)                              a plain value
         ('tern', raw_true, cond_ast, else_node)   a ternary
         ('keep',)                                 the missing else of a
                                                   condition-only assignment
    Raises ConditionError on a malformed ternary or condition.
    """
    idx = find_top_level(expr, " if ")
    if idx is None:
        # A trailing bare "if" has no " if " to find, so it would quietly become
        # literal text. Now that the else is optional, that is a likely typo.
        if find_top_level(expr.rstrip() + " ", " if ") is not None:
            raise ConditionError("ternary 'if' without a condition in '%s'" % expr.strip())
        return ("val", expr)
    value_true = expr[:idx].strip()      # so aligned "X = \"a\"   if ..." keeps no padding
    rest = expr[idx + 4:]
    eidx = find_top_level(rest, " else ")
    if eidx is None:
        # "NAME = value if <cond>" with no else: assign only when the condition
        # holds, otherwise leave NAME as it stands. Successive lines then read
        # as if / elif instead of each one overwriting the last.
        return ("tern", value_true, parse_condition(rest.strip()), ("keep",))
    cond = rest[:eidx].strip()
    return ("tern", value_true, parse_condition(cond), parse_vexpr(rest[eidx + 6:].strip()))


def eval_vexpr(node, ctx, current=""):
    """Evaluate a value expression. 'current' is what the variable being
    defined already holds -- the value a condition-only assignment keeps."""
    if node[0] == "val":
        return assemble_value(node[1], ctx)
    if node[0] == "keep":
        return current
    _, value_true, cond_ast, else_node = node
    if eval_condition_ast(cond_ast, ctx):
        return assemble_value(value_true, ctx)
    return eval_vexpr(else_node, ctx, current)


# --------------------------------------------------------------------------- #
# Config model
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self):
        self.settings = {"STABLE_SECONDS": "2"}
        self.required = []
        self.vars = []          # list of (name, vexpr node) in file order
        self.rules = []         # list of {lineno, text, cond, dest}
        self.errors = []
        self.warnings = []
        self.seen = set()

    def parse(self, path):
        """Read the config file into settings / vars / rules.

        Does not stop at the first problem: everything wrong is collected in
        .errors so a single --check reports all of it. A line that fails to
        parse still leaves a placeholder behind (a rule with no AST, a variable
        holding its raw text), so the lines after it are checked too instead of
        the first mistake hiding the rest.

        Each non-empty line is one of three kinds, tried in this order:
          1. a rule        <condition> => <destination>   (contains a top-level =>)
          2. a setting     NAME = value, NAME in RESERVED
          3. a variable    NAME = value, any other identifier
        Rules are recognised first because a rule also contains an "=", so the
        assignment test would otherwise swallow every rule in the file.
        """
        try:
            fh = open(path, "r", encoding="utf-8")
        except OSError:
            self.errors.append("cannot read config file: %s" % path)
            return
        with fh:
            for lineno, raw in enumerate(fh, 1):
                line = strip_inline_comment(raw.rstrip("\n")).strip()
                if not line:
                    continue
                if is_rule(line):
                    self._parse_rule(line, lineno)
                    continue
                eq = split_first_eq(line)
                if eq:
                    name, val = eq[0].strip(), eq[1].strip()
                    if not quotes_balanced(val):
                        self.errors.append("line %d: unbalanced quotes in value: %s" % (lineno, val))
                    if name in RESERVED:
                        self._assign_setting(name, val, lineno)
                    elif IDENT_RE.fullmatch(name):
                        try:
                            node = parse_vexpr(val)
                        except ConditionError as exc:
                            self.errors.append("line %d: %s" % (lineno, exc))
                            node = ("val", val)
                        self.vars.append((name, node))
                    else:
                        self.errors.append("line %d: invalid variable name '%s'" % (lineno, name))
                else:
                    self.errors.append("line %d: unrecognized line: %s" % (lineno, line))

    def _assign_setting(self, name, val, lineno):
        if name in self.seen:
            self.warnings.append("line %d: duplicate setting '%s' (last one wins)" % (lineno, name))
        self.seen.add(name)
        if name == "REQUIRED":
            self._parse_required(val, lineno)
            return
        self.settings[name] = unquote_simple(val)

    def _parse_required(self, val, lineno):
        for it in split_quote_aware(val, ","):
            it = it.strip()
            if not it:
                continue
            name = it
            if name.startswith("${") and name.endswith("}"):
                name = name[2:-1]
            elif name.startswith("$"):
                name = name[1:]
            if IDENT_RE.fullmatch(name):
                self.required.append(name)
            else:
                self.errors.append("line %d: invalid field in REQUIRED: '%s'" % (lineno, it))

    def _parse_rule(self, body, lineno):
        parts = split_quote_aware(body, "=>")
        if len(parts) != 2:
            self.errors.append("line %d: a rule must contain exactly one '=>'" % lineno)
            return
        cond, dest = parts[0].strip(), parts[1].strip()
        if not cond:
            self.errors.append("line %d: rule has an empty condition" % lineno)
        if not dest:
            self.errors.append("line %d: rule has an empty destination" % lineno)
        if not quotes_balanced(dest):
            self.errors.append("line %d: unbalanced quotes in destination" % lineno)
        ast = None
        if cond:
            try:
                ast = parse_condition(cond)
            except ConditionError as exc:
                self.errors.append("line %d: %s" % (lineno, exc))
        self.rules.append({"lineno": lineno, "text": body, "cond": cond, "dest": dest, "ast": ast})

    def validate(self):
        for req in ("INCOMING_DIR", "JSON_ARCHIVE_DIR", "LOG_DIR"):
            if not self.settings.get(req):
                self.errors.append("missing required setting: %s" % req)
        st = self.settings.get("STABLE_SECONDS", "2")
        if not re.fullmatch(r"[0-9]+", st):
            self.errors.append("STABLE_SECONDS must be a non-negative integer (got '%s')" % st)
        for name, what in (("LOG_MAX_MB", "a non-negative integer (0 disables rotation)"),
                           ("LOG_KEEP", "a non-negative integer"),
                           ("REPORT_KEEP_DAYS", "a non-negative integer (0 keeps every row)")):
            val = self.settings.get(name)
            if val is not None and not re.fullmatch(r"[0-9]+", val.strip()):
                self.errors.append("%s must be %s (got '%s')" % (name, what, val))
        sp = self.settings.get("REPORT_SPLIT", "none").strip().lower()
        if sp not in REPORT_SPLITS:
            self.errors.append("REPORT_SPLIT must be one of %s (got '%s')"
                               % (", ".join(REPORT_SPLITS), self.settings.get("REPORT_SPLIT")))
        for name in BOOL_SETTINGS:
            val = self.settings.get(name, "no")
            if val.strip().lower() not in BOOLS:
                self.errors.append("%s must be yes or no (got '%s')" % (name, val))
        if not self.rules:
            # Parses fine, dispatches nothing: every file would be logged as
            # "no rule matched" forever. Usually a rule swallowed by a quoting
            # slip, so say so rather than run a cron that can never do anything.
            self.errors.append("no rules defined: nothing can ever be dispatched "
                               "(a rule is: <condition> => \"<destination>\")")

    # ----------------------------------------------------------------------- #
    # Per-file resolution
    # ----------------------------------------------------------------------- #
    def resolve(self, jsonfile, debug, sysmeta=None):
        """Decide where one file goes, from its JSON sidecar.

        Returns a dict whose "status" the caller switches on:
          INVALID       the sidecar is not readable, or not a JSON object
          REQUIRED_FAIL a REQUIRED field is missing or empty ("missing" lists them)
          NOMATCH       no rule matched ("summary" describes the fields, for the log)
          UNSAFE        a rule matched but its destination is empty or has ".."
          OK            "dest" is where the file goes, "ruleno"/"ruletext" say why

        Three phases, in this order: read the fields, evaluate the variables on
        top of them, then try the rules. Nothing is cached between files -- each
        sidecar gets a fresh ctx, since every variable may depend on its fields.

        'sysmeta' is the filesystem's own view of the data file ($Filename and
        friends). It seeds ctx before the sidecar is read, so a sidecar field of
        the same name overrides it. 'jsonfile' may be None, for a data file
        dispatched on its system metadata alone.
        """
        trace = []
        data = {}
        if jsonfile is not None:
            try:
                with open(jsonfile, "r", encoding="utf-8") as jf:
                    data = json.load(jf)
            except (OSError, ValueError) as exc:
                # Carry the reason out: "invalid JSON" alone leaves the reader
                # opening the file by hand to find out what is wrong with it.
                return {"status": "INVALID", "cause": sanitize(str(exc)).replace("'", ""),
                        "debug": trace}
            if not isinstance(data, dict):
                return {"status": "INVALID", "debug": trace,
                        "cause": "top level is %s, expected an object" % type(data).__name__}

        ctx = Fields()
        for k, v in (sysmeta or {}).items():
            ctx[k] = sanitize(to_str(v))
        for k in data:                       # the sidecar has the last word
            ctx[k] = sanitize(to_str(data[k]))
        ctx.nulls = frozenset(k for k in data if data[k] is None)
        # The sidecar's own fields lead: they are what explains a non-match,
        # while the system ones are there for every file anyway.
        keys = list(data) + [k for k in (sysmeta or {}) if k not in data]
        if debug:
            for k in keys:
                trace.append("  %s field: $%s = %s"
                             % ("json" if k in data else "system", k,
                                "null" if k in ctx.nulls else "'%s'" % ctx[k]))

        # REQUIRED is a contract on what the sidecar must say, so it is only
        # checked when there is a sidecar. A file dispatched on its system
        # metadata alone has no such contract to honour -- checking it there
        # would make REQUIRED and DISPATCH_WITHOUT_JSON mutually exclusive,
        # since every sidecar-less file would fail on the very first field.
        # An explicit null counts as present: the producer said "no value here",
        # which is an answer. Absent and "" still fail.
        if jsonfile is not None:
            missing = [f for f in self.required if f not in ctx.nulls and not ctx.get(f)]
            if missing:
                return {"status": "REQUIRED_FAIL", "missing": missing, "debug": trace}

        for name, node in self.vars:
            ctx[name] = eval_vexpr(node, ctx, ctx.get(name, ""))
            if debug:
                trace.append("  variable: %s = '%s'" % (name, ctx[name]))

        for r in self.rules:
            if debug:
                trace.append("  rule #%d: %s => %s" % (r["lineno"], r["cond"], r["dest"]))
            if r["ast"] is not None and self._eval(r["ast"], ctx, debug, trace):
                dest = assemble_value(r["dest"], ctx)
                if debug:
                    trace.append("  -> MATCH; destination resolves to '%s'" % dest)
                if dest == "" or ".." in dest:
                    return {"status": "UNSAFE", "dest": dest, "debug": trace}
                return {"status": "OK", "dest": dest, "ruleno": r["lineno"],
                        "ruletext": r["text"], "debug": trace}
            if debug:
                trace.append("  -> no match")

        return {"status": "NOMATCH", "summary": self._summary(ctx, keys), "debug": trace}

    def _eval(self, node, ctx, debug, trace):
        kind = node[0]
        if kind == "ATOM":
            return self._atom_true(node[1], ctx, debug, trace)
        if kind == "AND":
            return self._eval(node[1], ctx, debug, trace) and self._eval(node[2], ctx, debug, trace)
        if kind == "OR":
            return self._eval(node[1], ctx, debug, trace) or self._eval(node[2], ctx, debug, trace)
        return False

    def _atom_true(self, at, ctx, debug, trace):
        if at["op"] in ("ISNULL", "ISNOTNULL"):
            res = is_null(at, ctx)
            if debug:
                trace.append("      atom $%s %s  -> %s"
                             % (at["field"], at["op"], "true" if res else "false"))
            return res
        lval = ctx.get(at["field"], "")
        if at["op"] != "IN":
            pat = assemble_value(at["rhs"], ctx)
            res = compare_atom(at["op"], lval, pat)
            if debug:
                trace.append("      atom $%s %s \"%s\"  ('%s')  -> %s"
                             % (at["field"], OP_SYM[at["op"]], pat, lval, "true" if res else "false"))
            return res
        for it in at["items"]:
            pat = assemble_value(it, ctx)
            if fnmatch.fnmatchcase(lval, pat):
                if debug:
                    trace.append("      atom $%s IN (...)  ('%s' == \"%s\")  -> true"
                                 % (at["field"], lval, pat))
                return True
        if debug:
            trace.append("      atom $%s IN (...)  ('%s' in none)  -> false" % (at["field"], lval))
        return False

    def _summary(self, ctx, keys):
        fields = self.required if self.required else keys
        parts, count = [], 0
        for k in fields:
            if count >= 6:
                parts.append("...")
                break
            parts.append("%s=%s" % (k, ctx.get(k, "")))
            count += 1
        return "fields: " + ", ".join(parts)
