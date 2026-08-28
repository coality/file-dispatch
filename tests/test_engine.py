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

    def test_parse_atom(self):
        self.assertEqual(engine.parse_atom('$x = "y"'), {"op": "EQ", "field": "x", "rhs": '"y"'})
        a = engine.parse_atom('$s IN ("a", "b")')
        self.assertEqual((a["op"], a["field"], a["items"]), ("IN", "s", ['"a"', '"b"']))
        self.assertIsNone(engine.parse_atom("x = y"))   # missing $
        self.assertIsNone(engine.parse_atom("$x"))      # no operator
        self.assertIsNone(engine.parse_atom("$x IN ()"))  # empty list


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


if __name__ == "__main__":
    unittest.main(verbosity=1)
