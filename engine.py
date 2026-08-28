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

Standard library only.
"""

import fnmatch
import json
import re

RESERVED = {
    "INCOMING_DIR", "JSON_ARCHIVE_DIR", "LOG_DIR",
    "STABLE_SECONDS", "REQUIRED", "PYTHON",
}
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


def assemble_value(s, ctx):
    """Concatenate adjacent segments: bare / "..." expand $refs, '...' is literal."""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == '"':
            j = i + 1
            while j < n and s[j] != '"':
                j += 1
            out.append(expand_refs(s[i + 1:j], ctx))
            i = j + 1
        elif c == "'":
            j = i + 1
            while j < n and s[j] != "'":
                j += 1
            out.append(s[i + 1:j])
            i = j + 1
        else:
            j = i
            while j < n and s[j] not in ('"', "'"):
                j += 1
            out.append(expand_refs(s[i:j], ctx))
            i = j
    return "".join(out)


def parse_atom(atom):
    """Parse '$field = value' or '$field IN (a, b)'. Return dict or None."""
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
    if rest.startswith("IN ") or rest.startswith("IN\t") or rest.startswith("IN("):
        rest = rest[2:].strip()
        if not (rest.startswith("(") and rest.endswith(")")):
            return None
        items = [it.strip() for it in split_quote_aware(rest[1:-1], ",")]
        if not items or any(it == "" for it in items):
            return None
        return {"op": "IN", "field": name, "items": items}
    if rest.startswith("!=") or rest.startswith("<>"):       # not-equal
        rhs = rest[2:].strip()
        if rhs == "":
            return None
        return {"op": "NE", "field": name, "rhs": rhs}
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
                    break
                depth -= 1; i += 1; continue
            if depth == 0 and ch in " \t":
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


def atom_matches(at, ctx):
    """Evaluate a parsed atom against ctx (used by rules and ternary conditions)."""
    lval = ctx.get(at["field"], "")
    if at["op"] == "EQ":
        return fnmatch.fnmatchcase(lval, assemble_value(at["rhs"], ctx))
    if at["op"] == "NE":
        return not fnmatch.fnmatchcase(lval, assemble_value(at["rhs"], ctx))
    return any(fnmatch.fnmatchcase(lval, assemble_value(it, ctx)) for it in at["items"])


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
    Raises ConditionError on a malformed ternary or condition.
    """
    idx = find_top_level(expr, " if ")
    if idx is None:
        return ("val", expr)
    value_true = expr[:idx]
    rest = expr[idx + 4:]
    eidx = find_top_level(rest, " else ")
    if eidx is None:
        raise ConditionError("ternary 'if' without matching 'else' in '%s'" % expr.strip())
    cond = rest[:eidx].strip()
    return ("tern", value_true, parse_condition(cond), parse_vexpr(rest[eidx + 6:]))


def eval_vexpr(node, ctx):
    if node[0] == "val":
        return assemble_value(node[1], ctx)
    _, value_true, cond_ast, else_node = node
    if eval_condition_ast(cond_ast, ctx):
        return assemble_value(value_true, ctx)
    return eval_vexpr(else_node, ctx)


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

    # ----------------------------------------------------------------------- #
    # Per-file resolution
    # ----------------------------------------------------------------------- #
    def resolve(self, jsonfile, debug):
        trace = []
        try:
            with open(jsonfile, "r", encoding="utf-8") as jf:
                data = json.load(jf)
        except (OSError, ValueError):
            return {"status": "INVALID", "debug": trace}
        if not isinstance(data, dict):
            return {"status": "INVALID", "debug": trace}

        ctx = {}
        keys = list(data.keys())
        for k in keys:
            ctx[k] = sanitize(to_str(data[k]))
        if debug:
            for k in keys:
                trace.append("  json field: $%s = '%s'" % (k, ctx[k]))

        missing = [f for f in self.required if not ctx.get(f)]
        if missing:
            return {"status": "REQUIRED_FAIL", "missing": missing, "debug": trace}

        for name, node in self.vars:
            ctx[name] = eval_vexpr(node, ctx)
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
        lval = ctx.get(at["field"], "")
        if at["op"] in ("EQ", "NE"):
            pat = assemble_value(at["rhs"], ctx)
            m = fnmatch.fnmatchcase(lval, pat)
            res = m if at["op"] == "EQ" else not m
            if debug:
                sym = "=" if at["op"] == "EQ" else "!="
                trace.append("      atom $%s %s \"%s\"  ('%s')  -> %s"
                             % (at["field"], sym, pat, lval, "true" if res else "false"))
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
