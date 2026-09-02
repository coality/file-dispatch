#!/usr/bin/env python3
"""Unit tests for engine.py (the parsing / matching core)."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import engine  # noqa: E402


class TestHelpers(unittest.TestCase):
    def test_split_quote_aware(self):
        self.assertEqual(engine.split_quote_aware("a AND b", " AND "), ["a", "b"])
        self.assertEqual(engine.split_quote_aware('"a AND b" AND c', " AND "), ['"a AND b"', "c"])
        self.assertEqual(engine.split_quote_aware('a,b,"c,d"', ","), ["a", "b", '"c,d"'])

    def test_quotes_balanced(self):
        self.assertTrue(engine.quotes_balanced('"a" b'))
        self.assertFalse(engine.quotes_balanced('"a'))

    def test_strip_inline_comment(self):
        self.assertEqual(engine.strip_inline_comment("x = 1 # c"), "x = 1 ")
        self.assertEqual(engine.strip_inline_comment('"a # b"'), '"a # b"')
        self.assertEqual(engine.strip_inline_comment("# whole line"), "")

    def test_unquote_simple(self):
        self.assertEqual(engine.unquote_simple('"/x"'), "/x")
        self.assertEqual(engine.unquote_simple("'/x'"), "/x")
        self.assertEqual(engine.unquote_simple("/x"), "/x")

    def test_expand_refs(self):
        self.assertEqual(engine.expand_refs("$a/$b", {"a": "X", "b": "Y"}), "X/Y")
        self.assertEqual(engine.expand_refs("${a}z", {"a": "X"}), "Xz")
        self.assertEqual(engine.expand_refs("$missing", {}), "")
        # a JSON value that looks like a command substitution stays literal
        self.assertEqual(engine.expand_refs("$a", {"a": "$(id)"}), "$(id)")

    def test_assemble_value(self):
        ctx = {"category": "report", "group": "B", "OUT": "/out"}
        self.assertEqual(engine.assemble_value('"$OUT/$group/reports"', ctx), "/out/B/reports")
        self.assertEqual(engine.assemble_value('$category"/toto"', ctx), "report/toto")
        self.assertEqual(engine.assemble_value('$group"/"$category', ctx), "B/report")
        self.assertEqual(engine.assemble_value("'$literal'", ctx), "$literal")

    def test_plus_concatenation(self):
        ctx = {"OUT": "/out", "group": "B", "category": "report"}
        # "+" joins segments and absorbs the spaces around it ...
        self.assertEqual(engine.assemble_value('$group + "/" + $category', ctx), "B/report")
        self.assertEqual(engine.assemble_value('$group+"/"+$category', ctx), "B/report")
        self.assertEqual(engine.assemble_value('"$OUT" + "/x"', ctx), "/out/x")
        # ... and mixes with juxtaposition and with functions.
        self.assertEqual(engine.assemble_value('$group"/" + $category', ctx), "B/report")
        self.assertEqual(engine.assemble_value('upper($group) + "-" + $category', ctx), "B-report")
        # A quoted "+" stays literal text.
        self.assertEqual(engine.assemble_value('"a+b"', ctx), "a+b")
        self.assertEqual(engine.assemble_value('"a" + "+" + "b"', ctx), "a+b")
        # A dangling "+" contributes nothing rather than blowing up.
        self.assertEqual(engine.assemble_value('+$group', ctx), "B")
        self.assertEqual(engine.assemble_value('$group +', ctx), "B")

    def test_plus_in_conditions_and_ternaries(self):
        ctx = {"a": "xy", "x": "x", "y": "y", "k": "1"}
        at = engine.parse_atom('$a = $x + $y')
        self.assertTrue(engine.atom_matches(at, ctx))
        node = engine.parse_vexpr('$x + "-ok" if $k = "1" else $x + "-ko"')
        self.assertEqual(engine.eval_vexpr(node, ctx), "x-ok")

    def test_parse_atom(self):
        self.assertEqual(engine.parse_atom('$x = "y"'), {"op": "EQ", "field": "x", "rhs": '"y"'})
        a = engine.parse_atom('$s IN ("a", "b")')
        self.assertEqual((a["op"], a["field"], a["items"]), ("IN", "s", ['"a"', '"b"']))
        self.assertIsNone(engine.parse_atom("x = y"))   # missing $
        self.assertIsNone(engine.parse_atom("$x"))      # no operator
        self.assertIsNone(engine.parse_atom("$x IN ()"))  # empty list

    def test_parse_condition_ast(self):
        ast = engine.parse_condition('$a = "x" AND $b = "y"')
        self.assertEqual(ast[0], "AND")
        ast2 = engine.parse_condition('($a = "x" OR $b = "y") AND $c = "z"')
        self.assertEqual(ast2[0], "AND")        # top operator is AND
        self.assertEqual(ast2[1][0], "OR")      # left operand is the grouped OR
        # grouping parens are not confused with IN's value-list parens
        ast3 = engine.parse_condition('$s IN ("a", "b") AND $t = "z"')
        self.assertEqual(ast3[0], "AND")

    def test_parse_condition_errors(self):
        for bad in ('($a = "x" AND $b = "y"', '$a = "x")', '$a = "x" AND', '(', '$a = "x" OR OR $b = "y"'):
            with self.assertRaises(engine.ConditionError):
                engine.parse_condition(bad)

    def test_parse_vexpr(self):
        self.assertEqual(engine.parse_vexpr('"a"'), ("val", '"a"'))
        node = engine.parse_vexpr('"a" if $x = "1" else "b"')
        self.assertEqual(node[0], "tern")
        self.assertEqual(node[3][0], "val")             # else branch is a plain value
        chained = engine.parse_vexpr('"a" if $k = "1" else "b" if $k = "2" else "c"')
        self.assertEqual(chained[0], "tern")
        self.assertEqual(chained[3][0], "tern")         # nested ternary in the else branch
        with self.assertRaises(engine.ConditionError):
            engine.parse_vexpr('"a" if $k = "1"')        # missing else

    def test_atom_accepts_double_equals(self):
        self.assertEqual(engine.parse_atom('$x == "y"'), {"op": "EQ", "field": "x", "rhs": '"y"'})

    def test_atom_not_equal(self):
        self.assertEqual(engine.parse_atom('$x != "y"'), {"op": "NE", "field": "x", "rhs": '"y"'})
        self.assertEqual(engine.parse_atom('$x <> "y"'), {"op": "NE", "field": "x", "rhs": '"y"'})

    def test_atom_numeric_ops(self):
        self.assertEqual(engine.parse_atom('$x < "10"')["op"], "LT")
        self.assertEqual(engine.parse_atom('$x > "10"')["op"], "GT")
        self.assertEqual(engine.parse_atom('$x <= "10"')["op"], "LE")
        self.assertEqual(engine.parse_atom('$x >= "10"')["op"], "GE")

    def test_atom_isnull_parsing(self):
        self.assertEqual(engine.parse_atom("$x ISNULL"), {"op": "ISNULL", "field": "x"})
        self.assertEqual(engine.parse_atom("$x ISNOTNULL"), {"op": "ISNOTNULL", "field": "x"})
        # It takes no right-hand side, so anything after it is not an atom.
        self.assertIsNone(engine.parse_atom('$x ISNULL "y"'))

    def test_atom_string_ops(self):
        self.assertEqual(engine.parse_atom('$x STARTSWITH "a"')["op"], "STARTSWITH")
        self.assertEqual(engine.parse_atom('$x ENDSWITH "z"')["op"], "ENDSWITH")
        self.assertEqual(engine.parse_atom('$x CONTAINS "m"')["op"], "CONTAINS")

    def test_value_functions(self):
        self.assertEqual(engine.assemble_value("int($x)", {"x": "007"}), "7")
        self.assertEqual(engine.assemble_value("int($x)", {"x": "3.9"}), "3")
        self.assertEqual(engine.assemble_value("int($x)", {"x": "abc"}), "abc")   # unchanged
        self.assertEqual(engine.assemble_value("upper($x)", {"x": "aB"}), "AB")
        self.assertEqual(engine.assemble_value("lower($x)", {"x": "aB"}), "ab")
        self.assertEqual(engine.assemble_value('"x-"int($n)"-y"', {"n": "05"}), "x-5-y")


class TestResolve(unittest.TestCase):
    def _config(self, text):
        fh = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
        fh.write(text)
        fh.close()
        cfg = engine.Config()
        cfg.parse(fh.name)
        cfg.validate()
        os.unlink(fh.name)
        return cfg

    def _json(self, obj):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(obj, fh)
        fh.close()
        return fh.name

    BASE = 'INCOMING_DIR="/i"\nJSON_ARCHIVE_DIR="/a"\nLOG_DIR="/l"\nOUT="/out"\n'

    def _resolve(self, cfg, obj):
        path = self._json(obj)
        try:
            return cfg.resolve(path, False)
        finally:
            os.unlink(path)

    def test_match_and_or_in_wildcard(self):
        cfg = self._config(
            self.BASE
            + '$type = "invoice*" AND $region IN ("EU", "UK") => "$OUT/eu"\n'
            + '$category = "report" => "$OUT/r"\n'
        )
        self.assertEqual(cfg.errors, [])
        self.assertEqual(self._resolve(cfg, {"type": "invoice_9", "region": "UK"})["dest"], "/out/eu")
        self.assertEqual(self._resolve(cfg, {"category": "report"})["dest"], "/out/r")
        self.assertEqual(self._resolve(cfg, {"type": "invoice_9", "region": "US"})["status"], "NOMATCH")

    def test_required_accepts_explicit_null(self):
        cfg = self._config(self.BASE + "REQUIRED = $bu, $status\n$bu = \"*\" => \"/o\"\n")
        # null is an answer: the producer said "no value here".
        self.assertEqual(self._resolve(cfg, {"bu": "B", "status": None})["status"], "OK")
        # absent and "" are not.
        self.assertEqual(self._resolve(cfg, {"bu": "B"})["status"], "REQUIRED_FAIL")
        self.assertEqual(self._resolve(cfg, {"bu": "B", "status": ""})["status"], "REQUIRED_FAIL")

    def test_isnull_and_isnotnull(self):
        cfg = self._config(self.BASE
                           + '$status ISNULL    => "/none"\n'
                           + '$status ISNOTNULL => "/some"\n')
        self.assertEqual(cfg.errors, [])
        self.assertEqual(self._resolve(cfg, {"status": None})["dest"], "/none")
        self.assertEqual(self._resolve(cfg, {"status": "done"})["dest"], "/some")
        # "" and an absent field are NOT null.
        self.assertEqual(self._resolve(cfg, {"status": ""})["dest"], "/some")
        self.assertEqual(self._resolve(cfg, {"other": "x"})["dest"], "/some")

    def test_isnull_in_ternary_and_with_and(self):
        cfg = self._config(self.BASE
                           + 'K = "none" if $e ISNULL else lower($e)\n'
                           + '$bu = "B" AND $e ISNULL => "/o/" + $K\n'
                           + '$bu = "*" => "/o/" + $K\n')
        self.assertEqual(self._resolve(cfg, {"bu": "B", "e": None})["dest"], "/o/none")
        self.assertEqual(self._resolve(cfg, {"bu": "C", "e": "DONE"})["dest"], "/o/done")

    def test_first_rule_wins(self):
        cfg = self._config(self.BASE + '$c = "x" => "/first"\n$c = "x" => "/second"\n')
        self.assertEqual(self._resolve(cfg, {"c": "x"})["dest"], "/first")

    def test_required_fail(self):
        cfg = self._config(self.BASE + "REQUIRED = $category, $group\n$category = \"r\" => \"/o\"\n")
        r = self._resolve(cfg, {"category": "r"})
        self.assertEqual(r["status"], "REQUIRED_FAIL")
        self.assertEqual(r["missing"], ["group"])

    def test_required_lists_all_missing(self):
        cfg = self._config(self.BASE + "REQUIRED = $a, $b\n$category = \"r\" => \"/o\"\n")
        self.assertEqual(self._resolve(cfg, {"category": "r"})["missing"], ["a", "b"])

    def test_empty_destination_is_unsafe(self):
        cfg = self._config(self.BASE + '$category = "r" => "$undefined"\n')
        self.assertEqual(self._resolve(cfg, {"category": "r"})["status"], "UNSAFE")

    def test_brace_and_single_quote_in_destination(self):
        cfg = self._config(self.BASE + "$category = \"r\" => \"$OUT/${group}\"/'lit'\n")
        self.assertEqual(self._resolve(cfg, {"category": "r", "group": "B"})["dest"], "/out/B/lit")

    def test_nomatch_summary_contains_fields(self):
        cfg = self._config(self.BASE + '$category = "report" => "/o"\n')
        r = self._resolve(cfg, {"category": "other", "group": "g"})
        self.assertEqual(r["status"], "NOMATCH")
        self.assertIn("category=other", r["summary"])

    def test_grouping_parentheses(self):
        cfg = self._config(self.BASE + '($cat = "order" OR $cat = "refund") AND $region = "EU" => "/o"\n')
        self.assertEqual(cfg.errors, [])
        self.assertEqual(self._resolve(cfg, {"cat": "order", "region": "EU"})["status"], "OK")
        self.assertEqual(self._resolve(cfg, {"cat": "refund", "region": "EU"})["status"], "OK")
        self.assertEqual(self._resolve(cfg, {"cat": "order", "region": "US"})["status"], "NOMATCH")

    def test_parentheses_change_precedence(self):
        # No parens: order OR (refund AND EU) -> "order" alone matches (AND binds first)
        no_parens = self._config(self.BASE + '$cat = "order" OR $cat = "refund" AND $region = "EU" => "/o"\n')
        self.assertEqual(self._resolve(no_parens, {"cat": "order", "region": "US"})["status"], "OK")
        # With parens: (order OR refund) AND EU -> "order" alone does NOT match
        parens = self._config(self.BASE + '($cat = "order" OR $cat = "refund") AND $region = "EU" => "/o"\n')
        self.assertEqual(self._resolve(parens, {"cat": "order", "region": "US"})["status"], "NOMATCH")

    def test_parentheses_with_in(self):
        cfg = self._config(self.BASE + '($s IN ("a", "b") OR $t = "x") AND $u = "y" => "/o"\n')
        self.assertEqual(self._resolve(cfg, {"s": "b", "u": "y"})["status"], "OK")
        self.assertEqual(self._resolve(cfg, {"t": "x", "u": "y"})["status"], "OK")
        self.assertEqual(self._resolve(cfg, {"s": "z", "u": "y"})["status"], "NOMATCH")

    def test_ternary_variable(self):
        cfg = self._config(self.BASE + 'T = "core" if $unit = "central" else $unit\n'
                                       '$category = "x" => "$OUT/$T"\n')
        self.assertEqual(cfg.errors, [])
        self.assertEqual(self._resolve(cfg, {"category": "x", "unit": "central"})["dest"], "/out/core")
        self.assertEqual(self._resolve(cfg, {"category": "x", "unit": "north"})["dest"], "/out/north")

    def test_ternary_python_eq_and_or(self):
        cfg = self._config(self.BASE + 'E = "prod" if $host == "p1" OR $host == "p2" else "dev"\n'
                                       '$category = "x" => "$OUT/$E"\n')
        self.assertEqual(self._resolve(cfg, {"category": "x", "host": "p2"})["dest"], "/out/prod")
        self.assertEqual(self._resolve(cfg, {"category": "x", "host": "z"})["dest"], "/out/dev")

    def test_ternary_chained(self):
        cfg = self._config(self.BASE + 'G = "a" if $k = "1" else "b" if $k = "2" else "c"\n'
                                       '$category = "x" => "$OUT/$G"\n')
        self.assertEqual(self._resolve(cfg, {"category": "x", "k": "1"})["dest"], "/out/a")
        self.assertEqual(self._resolve(cfg, {"category": "x", "k": "2"})["dest"], "/out/b")
        self.assertEqual(self._resolve(cfg, {"category": "x", "k": "9"})["dest"], "/out/c")

    def test_ternary_grouped_condition(self):
        cfg = self._config(self.BASE + 'T = "gold" if ($amt = "high" AND $vip = "yes") else "std"\n'
                                       '$category = "x" => "$OUT/$T"\n')
        self.assertEqual(self._resolve(cfg, {"category": "x", "amt": "high", "vip": "yes"})["dest"], "/out/gold")
        self.assertEqual(self._resolve(cfg, {"category": "x", "amt": "high", "vip": "no"})["dest"], "/out/std")

    def test_not_equal_operator(self):
        cfg = self._config(self.BASE + '$status != "done" => "$OUT/pending"\n')
        self.assertEqual(self._resolve(cfg, {"status": "open"})["dest"], "/out/pending")
        self.assertEqual(self._resolve(cfg, {"status": "done"})["status"], "NOMATCH")

    def test_not_equal_wildcard(self):
        cfg = self._config(self.BASE + '$name != "tmp*" => "$OUT/keep"\n')
        self.assertEqual(self._resolve(cfg, {"name": "report"})["dest"], "/out/keep")
        self.assertEqual(self._resolve(cfg, {"name": "tmp_1"})["status"], "NOMATCH")

    def test_not_equal_in_ternary(self):
        cfg = self._config(self.BASE + 'B = "x" if $a != "1" AND $b <> "2" else "y"\n'
                                       '$c = "*" => "$OUT/$B"\n')
        self.assertEqual(self._resolve(cfg, {"c": "z", "a": "9", "b": "9"})["dest"], "/out/x")
        self.assertEqual(self._resolve(cfg, {"c": "z", "a": "1", "b": "9"})["dest"], "/out/y")

    def test_numeric_operators(self):
        cfg = self._config(self.BASE + '$amount >= "100" AND $amount < "1000" => "$OUT/mid"\n')
        self.assertEqual(self._resolve(cfg, {"amount": "100"})["dest"], "/out/mid")
        self.assertEqual(self._resolve(cfg, {"amount": "999"})["dest"], "/out/mid")
        self.assertEqual(self._resolve(cfg, {"amount": "50"})["status"], "NOMATCH")
        self.assertEqual(self._resolve(cfg, {"amount": "1000"})["status"], "NOMATCH")
        self.assertEqual(self._resolve(cfg, {"amount": "abc"})["status"], "NOMATCH")   # non-numeric

    def test_numeric_json_number(self):
        cfg = self._config(self.BASE + '$n > "5" => "$OUT/big"\n')
        self.assertEqual(self._resolve(cfg, {"n": 9})["dest"], "/out/big")
        self.assertEqual(self._resolve(cfg, {"n": 2})["status"], "NOMATCH")

    def test_string_operators(self):
        cfg = self._config(self.BASE + '$name STARTSWITH "inv" => "$OUT/a"\n'
                                       '$name ENDSWITH ".xml" => "$OUT/b"\n'
                                       '$name CONTAINS "2026" => "$OUT/c"\n')
        self.assertEqual(self._resolve(cfg, {"name": "invoice"})["dest"], "/out/a")
        self.assertEqual(self._resolve(cfg, {"name": "data.xml"})["dest"], "/out/b")
        self.assertEqual(self._resolve(cfg, {"name": "r2026"})["dest"], "/out/c")
        self.assertEqual(self._resolve(cfg, {"name": "other"})["status"], "NOMATCH")

    def test_int_cast_in_destination(self):
        cfg = self._config(self.BASE + '$k = "*" => "$OUT/shard-"int($id)\n')
        self.assertEqual(self._resolve(cfg, {"k": "z", "id": "007"})["dest"], "/out/shard-7")

    def test_unsafe_destination(self):
        cfg = self._config(self.BASE + 'D = "$group"\n$category = "r" => "$OUT/$D/x"\n')
        self.assertEqual(self._resolve(cfg, {"category": "r", "group": "../../etc"})["status"], "UNSAFE")

    def test_invalid_json(self):
        cfg = self._config(self.BASE + '$c = "x" => "/o"\n')
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        fh.write("{not valid")
        fh.close()
        try:
            self.assertEqual(cfg.resolve(fh.name, False)["status"], "INVALID")
        finally:
            os.unlink(fh.name)


class TestConfigErrors(unittest.TestCase):
    def _parse(self, text):
        fh = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
        fh.write(text)
        fh.close()
        cfg = engine.Config()
        cfg.parse(fh.name)
        cfg.validate()
        os.unlink(fh.name)
        return cfg

    def test_missing_settings_and_empty_dest(self):
        cfg = self._parse('LOG_DIR="/l"\n$x = "y" =>\n')
        self.assertTrue(any("empty destination" in e for e in cfg.errors))
        self.assertTrue(any("INCOMING_DIR" in e for e in cfg.errors))

    def test_unbalanced_and_bad_stable(self):
        cfg = self._parse('INCOMING_DIR="/i"\nJSON_ARCHIVE_DIR="/a"\nLOG_DIR="/l"\nSTABLE_SECONDS=abc\n')
        self.assertTrue(any("STABLE_SECONDS" in e for e in cfg.errors))

    def test_error_has_line_number(self):
        cfg = self._parse('INCOMING_DIR="/i"\nJSON_ARCHIVE_DIR="/a"\nLOG_DIR="/l"\nnot a valid line\n')
        self.assertTrue(any("line 4" in e for e in cfg.errors))

    def test_unbalanced_parentheses(self):
        cfg = self._parse('INCOMING_DIR="/i"\nJSON_ARCHIVE_DIR="/a"\nLOG_DIR="/l"\n'
                          '($a = "x" AND $b = "y" => "/o"\n')
        self.assertTrue(any("')'" in e for e in cfg.errors), cfg.errors)

    def test_stray_close_parenthesis(self):
        cfg = self._parse('INCOMING_DIR="/i"\nJSON_ARCHIVE_DIR="/a"\nLOG_DIR="/l"\n'
                          '$a = "x") => "/o"\n')
        self.assertTrue(any("condition" in e for e in cfg.errors), cfg.errors)

    def test_ternary_missing_else(self):
        cfg = self._parse('INCOMING_DIR="/i"\nJSON_ARCHIVE_DIR="/a"\nLOG_DIR="/l"\n'
                          'X = "a" if $k = "1"\n')
        self.assertTrue(any("else" in e for e in cfg.errors), cfg.errors)


if __name__ == "__main__":
    unittest.main(verbosity=1)
