#!/usr/bin/env python3
"""End-to-end tests for file-dispatch.

Each test builds an isolated sandbox, writes a dispatch.conf, drops files in the
incoming directory, runs the real entry point (./dispatch.sh) as a subprocess,
and asserts the final state: file locations, log contents, and exit code.
"""

import csv
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DISPATCH = os.path.join(ROOT, "dispatch.sh")


class E2EBase(unittest.TestCase):
    def setUp(self):
        self.sb = tempfile.mkdtemp(prefix="fd-e2e-")
        self.incoming = os.path.join(self.sb, "incoming")
        self.archive = os.path.join(self.sb, "archive")
        self.out = os.path.join(self.sb, "out")
        self.logdir = os.path.join(self.sb, "logs")
        self.logf = os.path.join(self.logdir, "dispatch.log")
        self.errf = os.path.join(self.logdir, "errors.log")
        self.conf = os.path.join(self.sb, "dispatch.conf")
        for d in (self.incoming, self.out, self.logdir):
            os.makedirs(d)

    def tearDown(self):
        for root, dirs, _files in os.walk(self.sb):
            for d in dirs:
                try:
                    os.chmod(os.path.join(root, d), 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.sb, ignore_errors=True)

    # -- config / files -----------------------------------------------------
    def write_conf(self, block, stable=0, extra=""):
        with open(self.conf, "w") as f:
            f.write('INCOMING_DIR = "%s"\n' % self.incoming)
            f.write('JSON_ARCHIVE_DIR = "%s"\n' % self.archive)
            f.write('LOG_DIR = "%s"\n' % self.logdir)
            f.write("STABLE_SECONDS = %s\n" % stable)
            # These tests are about routing, not about directory policy, so they
            # opt in to creation; CREATE_DIRS defaults to no (tests 73-74).
            f.write("CREATE_DIRS = yes\n")
            f.write('OUT = "%s"\n' % self.out)
            if extra:
                f.write(extra + "\n")
            f.write(block + "\n")

    def write_raw(self, path, text):
        with open(path, "w") as f:
            f.write(text)

    def mkpair(self, name, ext, jsontext, data="DATA"):
        self.write_raw(os.path.join(self.incoming, "%s.%s" % (name, ext)), data)
        self.write_raw(os.path.join(self.incoming, "%s.json" % name), jsontext)

    def mkjson_obj(self, name, ext, obj, data="DATA"):
        self.mkpair(name, ext, json.dumps(obj), data)

    # -- running ------------------------------------------------------------
    def run_args(self, args, env=None):
        e = dict(os.environ)
        if env:
            e.update(env)
        return subprocess.run([DISPATCH, *args], capture_output=True, text=True, env=e)

    def dispatch(self, *flags, env=None, config=None):
        cfg = self.conf if config is None else config
        return self.run_args([*flags, cfg], env=env)

    # -- assertions ---------------------------------------------------------
    def log(self):
        try:
            with open(self.logf) as f:
                return f.read()
        except OSError:
            return ""

    def errlog(self):
        try:
            with open(self.errf) as f:
                return f.read()
        except OSError:
            return ""

    def inc(self, *p):
        return os.path.join(self.incoming, *p)

    def op(self, *p):
        return os.path.join(self.out, *p)

    def exists(self, p):
        self.assertTrue(os.path.exists(p), "missing: %s" % p)

    def absent(self, p):
        self.assertFalse(os.path.exists(p), "should be gone: %s" % p)

    def in_log(self, s):
        self.assertIn(s, self.log(), "log missing: %s" % s)

    def no_files_under(self, d):
        for _root, _dirs, files in os.walk(d):
            if files:
                self.fail("unexpected files under %s: %s" % (d, files))


class TestE2E(E2EBase):
    # 01
    def test_01_simple_match(self):
        self.write_conf('$category = "report" => "$OUT/reports"')
        self.mkpair("example", "xml", '{"category":"report","group":"alpha"}')
        r = self.dispatch()
        self.exists(self.op("reports", "example.xml"))
        self.exists(os.path.join(self.archive, "example.json"))
        self.absent(self.inc("example.xml"))
        self.absent(self.inc("example.json"))
        self.in_log("SUCCESS move source=")
        self.in_log('(rule #7: $category = "report" => "$OUT/reports")')
        self.assertEqual(r.returncode, 0)

    # 02
    def test_02_variable_composition(self):
        self.write_conf('GROUP = "$group"\n$category = "report" => "$OUT/$GROUP/reports"')
        self.mkpair("ex", "xml", '{"category":"report","group":"B"}')
        self.dispatch()
        self.exists(self.op("B", "reports", "ex.xml"))

    # 03
    def test_03_field_routes_to_different_dirs(self):
        self.write_conf('GROUP = "$group"\n$category = "report" => "$OUT/$GROUP/reports"')
        self.mkpair("a", "xml", '{"category":"report","group":"X"}')
        self.mkpair("b", "xml", '{"category":"report","group":"Y"}')
        self.dispatch()
        self.exists(self.op("X", "reports", "a.xml"))
        self.exists(self.op("Y", "reports", "b.xml"))

    # 04
    def test_04_first_rule_wins(self):
        self.write_conf('$category = "report" => "$OUT/first"\n$category = "report" => "$OUT/second"')
        self.mkpair("a", "xml", '{"category":"report"}')
        self.dispatch()
        self.exists(self.op("first", "a.xml"))
        self.absent(self.op("second", "a.xml"))

    # 05
    def test_05_and(self):
        self.write_conf('$type = "export" AND $status = "new" => "$OUT/exp"')
        self.mkpair("a", "xml", '{"type":"export","status":"old"}')
        self.mkpair("b", "xml", '{"type":"export","status":"new"}')
        self.dispatch()
        self.exists(self.inc("a.xml"))
        self.exists(self.op("exp", "b.xml"))

    # 06
    def test_06_wildcard_any(self):
        self.write_conf('$anything = "*" => "$OUT/all"')
        self.mkpair("a", "xml", '{"anything":"whatever"}')
        self.dispatch()
        self.exists(self.op("all", "a.xml"))

    # 07
    def test_07_no_rule(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.mkpair("a", "xml", '{"category":"other","group":"g"}')
        self.dispatch()
        self.exists(self.inc("a.xml"))
        self.exists(self.inc("a.json"))
        self.in_log("no rule matched source=")

    # 08
    def test_08_invalid_json(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.write_raw(self.inc("bad.xml"), "DATA")
        self.write_raw(self.inc("bad.json"), "{not valid")
        self.mkpair("good", "xml", '{"category":"report"}')
        self.dispatch()
        self.exists(self.inc("bad.xml"))
        self.exists(self.inc("bad.json"))
        self.in_log("reason='invalid JSON'")
        self.exists(self.op("r", "good.xml"))

    # 09
    def test_09_json_without_data(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.write_raw(self.inc("lonely.json"), '{"category":"report"}')
        r = self.dispatch()
        self.exists(self.inc("lonely.json"))
        self.in_log("waiting for data file source=")
        self.assertEqual(r.returncode, 0)

    # 10
    def test_10_ambiguous(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.write_raw(self.inc("dup.xml"), "A")
        self.write_raw(self.inc("dup.csv"), "B")
        self.write_raw(self.inc("dup.json"), '{"category":"report"}')
        self.dispatch()
        self.exists(self.inc("dup.xml"))
        self.exists(self.inc("dup.csv"))
        self.in_log("ambiguous")

    # 11
    def test_11_path_traversal_guard(self):
        self.write_conf('DIR = "$group"\n$category = "report" => "$OUT/$DIR/x"')
        self.mkpair("a", "xml", '{"category":"report","group":"../../etc"}')
        self.dispatch()
        self.exists(self.inc("a.xml"))
        self.in_log("unsafe or empty destination")

    # 12
    def test_12_various_extensions(self):
        self.write_conf('$category = "data" => "$OUT/d"')
        self.mkpair("a", "xml", '{"category":"data"}')
        self.mkpair("b", "csv", '{"category":"data"}')
        self.mkpair("c", "txt", '{"category":"data"}')
        self.dispatch()
        self.exists(self.op("d", "a.xml"))
        self.exists(self.op("d", "b.csv"))
        self.exists(self.op("d", "c.txt"))

    # 13
    def test_13_json_archived_single_dir(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.mkpair("a", "xml", '{"category":"report"}')
        self.dispatch()
        self.exists(os.path.join(self.archive, "a.json"))

    # 14
    def test_14_destination_collision(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        os.makedirs(self.op("r"))
        self.write_raw(self.op("r", "a.xml"), "OLD")
        self.mkpair("a", "xml", '{"category":"report"}', data="NEW")
        self.dispatch()
        with open(self.op("r", "a.xml")) as f:
            self.assertEqual(f.read(), "OLD")
        suffixed = [n for n in os.listdir(self.op("r")) if n.startswith("a.xml.")]
        self.assertTrue(suffixed, "no suffixed copy created")

    # 15
    def test_15_idempotence(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.mkpair("a", "xml", '{"category":"report"}')
        self.mkpair("b", "xml", '{"category":"other"}')
        self.dispatch()
        self.dispatch()
        self.absent(self.inc("a.xml"))
        self.exists(self.inc("b.xml"))
        self.assertEqual([n for n in os.listdir(self.op("r")) if n == "a.xml"], ["a.xml"])

    # 16
    def test_16_in_membership(self):
        self.write_conf('$category IN ("invoice", "credit") => "$OUT/bill"')
        self.mkpair("a", "xml", '{"category":"credit"}')
        self.mkpair("b", "xml", '{"category":"debit"}')
        self.dispatch()
        self.exists(self.op("bill", "a.xml"))
        self.exists(self.inc("b.xml"))

    # 17
    def test_17_wildcard_in_values(self):
        self.write_conf('$name = "invoice*" => "$OUT/inv"\n$name = "*credit" => "$OUT/cred"')
        self.mkpair("a", "xml", '{"name":"invoice_2026"}')
        self.mkpair("b", "xml", '{"name":"pre_credit"}')
        self.mkpair("c", "xml", '{"name":"other"}')
        self.dispatch()
        self.exists(self.op("inv", "a.xml"))
        self.exists(self.op("cred", "b.xml"))
        self.exists(self.inc("c.xml"))

    # 18
    def test_18_or_precedence(self):
        self.write_conf('$cat = "order" AND $region = "US" OR $cat = "order" AND $region = "CA" => "$OUT/na"')
        self.mkpair("a", "xml", '{"cat":"order","region":"CA"}')
        self.mkpair("b", "xml", '{"cat":"order","region":"FR"}')
        self.dispatch()
        self.exists(self.op("na", "a.xml"))
        self.exists(self.inc("b.xml"))

    # 19
    def test_19_value_not_executed(self):
        self.write_conf('$category = "report" => "$OUT/$evil"')
        self.mkjson_obj("a", "xml", {"category": "report", "evil": "$(touch %s/pwned)" % self.sb})
        self.dispatch()
        self.absent(os.path.join(self.sb, "pwned"))

    # 20
    def test_20_hostile_filename(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.write_raw(self.inc("-rf weird.xml"), "D")
        self.write_raw(self.inc("-rf weird.json"), '{"category":"report"}')
        r = self.dispatch()
        self.exists(self.op("r", "-rf weird.xml"))
        self.assertEqual(r.returncode, 0)

    # 21
    def test_21_symlink_rejected(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.write_raw(os.path.join(self.sb, "realdata"), "REAL")
        os.symlink(os.path.join(self.sb, "realdata"), self.inc("link.xml"))
        self.write_raw(self.inc("link.json"), '{"category":"report"}')
        self.dispatch()
        self.exists(self.inc("link.xml"))
        self.in_log("symlink")

    # 22
    def test_22_newline_no_log_injection(self):
        self.write_conf('$category = "nomatch" => "$OUT/r"')
        self.mkjson_obj("a", "xml", {"category": "evil\ninjected", "group": "g"})
        self.dispatch()
        self.in_log("no rule matched source=")
        for line in self.log().splitlines():
            self.assertFalse(line.startswith("injected"), "forged log line: %r" % line)

    # 23
    def test_23_required_missing(self):
        self.write_conf('$category = "report" => "$OUT/r"', extra="REQUIRED = $category, $group")
        self.mkpair("a", "xml", '{"category":"report"}')
        self.dispatch()
        self.exists(self.inc("a.xml"))
        self.in_log("missing/empty required field(s): group")

    # 24
    def test_24_required_empty(self):
        self.write_conf('$category = "report" => "$OUT/r"', extra="REQUIRED = $category, $group")
        self.mkpair("a", "xml", '{"category":"report","group":""}')
        self.dispatch()
        self.exists(self.inc("a.xml"))
        self.in_log("missing/empty required field(s): group")

    # 25
    def test_25_data_without_json(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.write_raw(self.inc("orphan.csv"), "D")
        self.dispatch()
        self.exists(self.inc("orphan.csv"))
        self.in_log("waiting for metadata source=")

    # 26
    def test_26_file_still_writing(self):
        self.write_conf('$category = "report" => "$OUT/r"', stable=1)
        self.write_raw(self.inc("w.json"), '{"category":"report"}')
        data = self.inc("w.xml")
        self.write_raw(data, "start")
        stop = threading.Event()

        def writer():
            while not stop.is_set():
                try:
                    with open(data, "a") as f:
                        f.write("x")
                except OSError:
                    return
                time.sleep(0.05)

        t = threading.Thread(target=writer)
        t.start()
        try:
            self.dispatch()
        finally:
            stop.set()
            t.join()
        self.exists(self.inc("w.xml"))
        self.in_log("still changing")

    # 27
    def test_27_quotes_spaces_commas(self):
        self.write_conf('$status IN ("in progress", "on hold") => "$OUT/pending files"\n'
                        '$title = "a,b" => "$OUT/comma"')
        self.mkpair("a", "xml", '{"status":"on hold","title":"z"}')
        self.mkpair("b", "xml", '{"status":"none","title":"a,b"}')
        self.dispatch()
        self.exists(self.op("pending files", "a.xml"))
        self.exists(self.op("comma", "b.xml"))

    # 28
    def test_28_segment_concatenation(self):
        self.write_conf('RET = $category"/sub"\n$category = "report" => "$OUT/$RET"')
        self.mkpair("a", "xml", '{"category":"report"}')
        self.dispatch()
        self.exists(self.op("report", "sub", "a.xml"))

    # 29
    def test_29_preflight_invalid_config(self):
        self.write_raw(self.conf,
                       'INCOMING_DIR = "%s"\nJSON_ARCHIVE_DIR = "%s"\nLOG_DIR = "%s"\n'
                       '$category = "report" =>\nthis is not a valid line\n'
                       % (self.incoming, self.archive, self.logdir))
        self.mkpair("a", "xml", '{"category":"report"}')
        r = self.dispatch()
        self.assertEqual(r.returncode, 2)
        self.exists(self.inc("a.xml"))
        self.no_files_under(self.out)
        import re
        self.assertTrue(re.search(r"line \d+", self.log()), "no line number in config errors")

    # 30
    def test_30_check(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.mkpair("a", "xml", '{"category":"report"}')
        r_ok = self.run_args(["--check", self.conf])
        self.assertEqual(r_ok.returncode, 0)
        self.exists(self.inc("a.xml"))
        bad = os.path.join(self.sb, "bad.conf")
        self.write_raw(bad, "this is broken\n")
        r_bad = self.run_args(["--check", bad])
        self.assertNotEqual(r_bad.returncode, 0)

    # 31
    def test_31_dry_run(self):
        self.write_conf('$category = "report" => "$OUT/reports"')
        self.mkpair("a", "xml", '{"category":"report"}')
        r = self.dispatch("--dry-run")
        self.assertEqual(r.returncode, 0)
        self.exists(self.inc("a.xml"))
        self.exists(self.inc("a.json"))
        self.no_files_under(self.out)
        self.in_log("DRY-RUN mode: no files will be moved")
        self.in_log("DRY-RUN SUCCESS move source=")

    # 32
    def test_32_logs_split(self):
        self.write_conf('$category = "report" => "$OUT/r"', extra="REQUIRED = $category")
        self.mkpair("good", "xml", '{"category":"report"}')
        self.mkpair("bad", "xml", '{"other":"x"}')
        self.dispatch()
        self.exists(self.logf)
        self.exists(self.errf)
        self.assertIn("missing/empty required field", self.errlog())
        self.assertNotIn("SUCCESS", self.errlog())
        self.assertIn("SUCCESS", self.log())
        self.assertIn("missing/empty required field", self.log())

    # 33
    def test_33_config_file_flag(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.mkpair("a", "xml", '{"category":"report"}')
        self.assertEqual(self.run_args(["--config-file", self.conf, "--check"]).returncode, 0)
        self.assertEqual(self.run_args(["--config-file=" + self.conf, "--check"]).returncode, 0)
        self.run_args(["--config-file", self.conf])
        self.exists(self.op("r", "a.xml"))

    # 34
    def test_34_env_var_and_precedence(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.mkpair("a", "xml", '{"category":"report"}')
        self.run_args([], env={"DISPATCH_CONFIG": self.conf})
        self.exists(self.op("r", "a.xml"))
        # --config-file overrides $DISPATCH_CONFIG
        self.mkpair("b", "xml", '{"category":"report"}')
        bad = os.path.join(self.sb, "bad.conf")
        self.write_raw(bad, "this is broken\n")
        r = self.run_args(["--config-file", self.conf], env={"DISPATCH_CONFIG": bad})
        self.assertEqual(r.returncode, 0)
        self.exists(self.op("r", "b.xml"))

    # 35
    def test_35_flock_concurrent(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.mkpair("a", "xml", '{"category":"report"}')
        lock = os.path.join(self.logdir, ".dispatch.lock")
        fd = open(lock, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            r = self.dispatch()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
        self.assertEqual(r.returncode, 0)
        self.exists(self.inc("a.xml"))
        self.in_log("another instance is already running")

    # 36
    def test_36_stable_seconds_non_numeric(self):
        self.write_raw(self.conf,
                       'INCOMING_DIR = "%s"\nJSON_ARCHIVE_DIR = "%s"\nLOG_DIR = "%s"\n'
                       'STABLE_SECONDS = abc\n$category = "report" => "%s/r"\n'
                       % (self.incoming, self.archive, self.logdir, self.out))
        self.mkpair("a", "xml", '{"category":"report"}')
        r = self.dispatch()
        self.assertEqual(r.returncode, 2)
        self.exists(self.inc("a.xml"))
        self.in_log("STABLE_SECONDS")

    # 37
    def test_37_help(self):
        r = self.run_args(["--help"])
        self.assertEqual(r.returncode, 0)
        out = r.stdout + r.stderr
        self.assertIn("--config-file", out)
        self.assertIn("DISPATCH_CONFIG", out)

    # 38
    def test_38_mega_complex(self):
        self.write_conf('$type = "invoice*" AND $region IN ("EU", "UK") AND $status = "new" '
                        'OR $priority = "high" AND $flag IN ("urgent", "on hold") => "$OUT/complex"')
        self.mkpair("a", "xml", '{"type":"invoice_2026","region":"EU","status":"new"}')
        self.mkpair("b", "xml", '{"type":"invoice_x","region":"US","status":"new"}')
        self.mkpair("c", "xml", '{"priority":"high","flag":"on hold"}')
        self.mkpair("d", "xml", '{"priority":"high","flag":"low"}')
        self.mkpair("e", "xml", '{"type":"creditnote","region":"EU","status":"new"}')
        self.dispatch()
        self.exists(self.op("complex", "a.xml"))
        self.exists(self.op("complex", "c.xml"))
        self.exists(self.inc("b.xml"))
        self.exists(self.inc("d.xml"))
        self.exists(self.inc("e.xml"))

    # 39
    def test_39_three_or_groups(self):
        self.write_conf('$a = "x" AND $b = "y" OR $c IN ("p", "q", "r") AND $d = "z*" '
                        'OR $e = "solo" => "$OUT/multi"')
        self.mkpair("j", "xml", '{"c":"q","d":"zebra"}')
        self.mkpair("k", "xml", '{"e":"solo"}')
        self.mkpair("l", "xml", '{"a":"x","b":"no"}')
        self.dispatch()
        self.exists(self.op("multi", "j.xml"))
        self.exists(self.op("multi", "k.xml"))
        self.exists(self.inc("l.xml"))

    # 40
    def test_40_success_line_fields(self):
        self.write_conf('$category = "report" => "$OUT/reports"')
        self.mkpair("a", "xml", '{"category":"report"}')
        self.dispatch()
        self.in_log("SUCCESS move source=")
        self.in_log("source='%s'" % self.inc("a.xml"))
        self.in_log("dest='%s'" % self.op("reports"))
        self.in_log("target='%s'" % self.op("reports", "a.xml"))

    # 41
    def test_41_failure_status(self):
        self.write_conf('$category = "report" => "$OUT/blocked/sub"')
        self.write_raw(self.op("blocked"), "X")  # a file where a directory is needed
        self.mkpair("a", "xml", '{"category":"report"}')
        self.dispatch()
        self.exists(self.inc("a.xml"))
        self.in_log("FAILURE move source=")
        self.in_log("cannot create destination")

    # 42
    def test_42_debug_trace(self):
        self.write_conf('GROUP = "$group"\n$category = "report" => "$OUT/$GROUP/reports"')
        self.mkpair("a", "xml", '{"category":"report","group":"B"}')
        self.dispatch("--debug")
        self.in_log("[DEBUG]")
        self.in_log("json field: $category = 'report'")
        self.in_log("variable: GROUP = 'B'")
        self.in_log("MATCH; destination resolves to")
        self.exists(self.op("B", "reports", "a.xml"))

    # 43
    def test_43_python_setting(self):
        py = shutil.which("python3") or sys.executable
        self.write_conf('$category = "report" => "$OUT/r"', extra='PYTHON = "%s"' % py)
        self.mkpair("a", "xml", '{"category":"report"}')
        r = self.dispatch()
        self.assertEqual(r.returncode, 0)
        self.exists(self.op("r", "a.xml"))

    # 44
    def test_44_bad_python_setting(self):
        self.write_conf('$category = "report" => "$OUT/r"', extra='PYTHON = "/nonexistent/python-xyz"')
        self.mkpair("a", "xml", '{"category":"report"}')
        r = self.dispatch()
        self.assertEqual(r.returncode, 3)
        self.exists(self.inc("a.xml"))

    # --- logging coverage --------------------------------------------------

    # 45: the run summary line is logged, with correct counts
    def test_45_run_summary_logged(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.mkpair("a", "xml", '{"category":"report"}')
        self.dispatch()
        self.in_log("run summary: processed=1 unmatched=0 invalid=0 incomplete=0 unstable=0 errors=0")

    # 46: a no-match line reports the JSON field values that were checked
    def test_46_nomatch_logs_fields(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.mkpair("a", "xml", '{"category":"other","group":"g"}')
        self.dispatch()
        self.in_log("no rule matched source=")
        self.in_log("fields: category=other")

    # 47: REQUIRED failure lists every missing field
    def test_47_required_lists_all_missing(self):
        self.write_conf('$category = "report" => "$OUT/r"', extra="REQUIRED = $a, $b")
        self.mkpair("x", "xml", '{"category":"report"}')
        self.dispatch()
        self.in_log("missing/empty required field(s): a, b")

    # 48: a duplicate setting is a WARNING, and the run still proceeds
    def test_48_duplicate_setting_warns(self):
        self.write_conf('$category = "report" => "$OUT/r"', extra="STABLE_SECONDS = 0")
        self.mkpair("a", "xml", '{"category":"report"}')
        r = self.dispatch()
        self.assertEqual(r.returncode, 0)
        self.in_log("duplicate setting 'STABLE_SECONDS'")
        self.exists(self.op("r", "a.xml"))          # processing still happened

    # 49: config errors go to errors.log and stderr (not only dispatch.log)
    def test_49_config_error_channels(self):
        self.write_raw(self.conf,
                       'INCOMING_DIR = "%s"\nJSON_ARCHIVE_DIR = "%s"\nLOG_DIR = "%s"\n'
                       'bogus line here\n' % (self.incoming, self.archive, self.logdir))
        r = self.dispatch()
        self.assertEqual(r.returncode, 2)
        self.assertIn("config:", self.errlog())
        self.assertIn("config:", r.stderr)
        self.assertIn("config:", self.log())

    # 50: a missing incoming directory is a logged error with exit code 1
    def test_50_incoming_dir_missing(self):
        missing = os.path.join(self.sb, "nope")
        self.write_raw(self.conf,
                       'INCOMING_DIR = "%s"\nJSON_ARCHIVE_DIR = "%s"\nLOG_DIR = "%s"\n'
                       'STABLE_SECONDS = 0\n$category = "report" => "%s/r"\n'
                       % (missing, self.archive, self.logdir, self.out))
        r = self.dispatch()
        self.assertEqual(r.returncode, 1)
        self.in_log("incoming directory does not exist")

    # 51: a destination that resolves to empty (undefined var) is refused
    def test_51_empty_destination(self):
        self.write_conf('$category = "report" => "$undefinedvar"')
        self.mkpair("a", "xml", '{"category":"report"}')
        self.dispatch()
        self.exists(self.inc("a.xml"))
        self.in_log("unsafe or empty destination")

    # 52: every log line has an ISO timestamp and a level
    def test_52_log_line_format(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.mkpair("a", "xml", '{"category":"report"}')
        self.dispatch()
        import re
        pat = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4} \[(INFO|WARN|ERROR|DEBUG)\] ")
        lines = [ln for ln in self.log().splitlines() if ln]
        self.assertTrue(lines)
        for ln in lines:
            self.assertRegex(ln, pat)

    # 53: archiving a JSON whose name already exists keeps the old one (suffix)
    def test_53_archive_collision(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        os.makedirs(self.archive)
        self.write_raw(os.path.join(self.archive, "a.json"), "OLD")
        self.mkpair("a", "xml", '{"category":"report"}')
        self.dispatch()
        with open(os.path.join(self.archive, "a.json")) as f:
            self.assertEqual(f.read(), "OLD")
        suffixed = [n for n in os.listdir(self.archive) if n.startswith("a.json.")]
        self.assertTrue(suffixed, "no suffixed archive copy created")

    # 54: ${brace} variable syntax works in a destination
    def test_54_brace_variable(self):
        self.write_conf('$category = "report" => "$OUT/${group}/reports"')
        self.mkpair("a", "xml", '{"category":"report","group":"B"}')
        self.dispatch()
        self.exists(self.op("B", "reports", "a.xml"))

    # 55: errors.log carries FAILURE lines but not INFO (SUCCESS / summary)
    def test_55_errorlog_levels(self):
        self.write_conf('$category = "report" => "$OUT/blocked/sub"')
        self.write_raw(self.op("blocked"), "X")
        self.mkpair("a", "xml", '{"category":"report"}')
        self.dispatch()
        self.assertIn("FAILURE", self.errlog())
        self.assertNotIn("run summary", self.errlog())
        self.assertNotIn("SUCCESS", self.errlog())

    # 56: --debug logs the pair being processed
    def test_56_debug_processing_pair(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.mkpair("a", "xml", '{"category":"report"}')
        self.dispatch("--debug")
        self.in_log("processing pair: source=")

    # 57: grouping parentheses in a rule condition
    def test_57_grouping_parentheses(self):
        self.write_conf('($category = "order" OR $category = "refund") AND $region = "EU" => "$OUT/eu"')
        self.mkpair("a", "xml", '{"category":"order","region":"EU"}')
        self.mkpair("b", "xml", '{"category":"refund","region":"EU"}')
        self.mkpair("c", "xml", '{"category":"order","region":"US"}')   # grouped -> still needs EU
        self.dispatch()
        self.exists(self.op("eu", "a.xml"))
        self.exists(self.op("eu", "b.xml"))
        self.exists(self.inc("c.xml"))

    # 58: ternary expression in a variable definition
    def test_58_ternary_variable(self):
        self.write_conf('T = "core" if $unit = "central" else $unit\n'
                        '$type = "*" => "$OUT/$T"')
        self.mkpair("a", "xml", '{"unit":"central","type":"t"}')
        self.mkpair("b", "xml", '{"unit":"north","type":"t"}')
        self.dispatch()
        self.exists(self.op("core", "a.xml"))
        self.exists(self.op("north", "b.xml"))

    # 59: not-equal operator (!= / <>)
    def test_59_not_equal_operator(self):
        self.write_conf('$status != "done" AND $kind <> "draft" => "$OUT/active"')
        self.mkpair("a", "xml", '{"status":"open","kind":"final"}')
        self.mkpair("b", "xml", '{"status":"done","kind":"final"}')
        self.dispatch()
        self.exists(self.op("active", "a.xml"))
        self.exists(self.inc("b.xml"))

    # 60: numeric + string operators combined
    def test_60_numeric_and_string_operators(self):
        self.write_conf('$amount >= "100" AND $name STARTSWITH "inv" => "$OUT/big-invoices"')
        self.mkpair("a", "xml", '{"amount":"250","name":"invoice_1"}')
        self.mkpair("b", "xml", '{"amount":"50","name":"invoice_2"}')    # amount too small
        self.mkpair("c", "xml", '{"amount":"250","name":"other"}')       # wrong prefix
        self.dispatch()
        self.exists(self.op("big-invoices", "a.xml"))
        self.exists(self.inc("b.xml"))
        self.exists(self.inc("c.xml"))

    # 61: int() cast in a variable used to build the destination
    def test_61_int_cast_variable(self):
        self.write_conf('SHARD = int($id)\n$type = "*" => "$OUT/$SHARD"')
        self.mkpair("a", "xml", '{"id":"007","type":"t"}')
        self.dispatch()
        self.exists(self.op("7", "a.xml"))

    # 62: the interpreter version floor is enforced (clean error, nothing moved).
    # MIN_PYTHON is monkeypatched high so the guard fires on any interpreter.
    def test_62_min_python_guard(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.mkpair("a", "xml", '{"category":"report"}')
        snippet = ("import dispatch, sys; dispatch.MIN_PYTHON = (99, 0); "
                   "sys.exit(dispatch.main([%r]))" % self.conf)
        e = dict(os.environ)
        e["PYTHONPATH"] = ROOT + os.pathsep + e.get("PYTHONPATH", "")
        r = subprocess.run([sys.executable, "-c", snippet],
                           capture_output=True, text=True, env=e, cwd=ROOT)
        self.assertEqual(r.returncode, 3)
        self.assertIn("requires Python", r.stderr)
        self.exists(self.inc("a.xml"))          # data file left in place


    # 63: --dry-run reports a destination that could not be created.
    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_63_dry_run_flags_uncreatable_destination(self):
        ro = os.path.join(self.sb, "ro")
        os.makedirs(ro)
        os.chmod(ro, 0o555)
        self.write_conf('$category = "report" => "%s/sub/dest"' % ro)
        self.mkpair("a", "xml", '{"category":"report"}')
        r = self.dispatch("--dry-run")
        self.assertEqual(r.returncode, 0)
        self.in_log("DRY-RUN FAILURE move source=")
        self.in_log("cannot create destination")
        self.in_log("errors=1")
        self.assertNotIn("SUCCESS move source=", self.log())
        self.assertIn("FAILURE move source=", self.errlog())
        self.exists(self.inc("a.xml"))          # dry-run still moves nothing

    # 64: --dry-run reports a destination directory that exists but is read-only.
    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_64_dry_run_flags_readonly_destination(self):
        dest = os.path.join(self.out, "locked")
        os.makedirs(dest)
        os.chmod(dest, 0o555)
        self.write_conf('$category = "report" => "$OUT/locked"')
        self.mkpair("a", "xml", '{"category":"report"}')
        self.dispatch("--dry-run")
        self.in_log("destination directory is not writable")
        self.in_log("errors=1")

    # 65: --dry-run reports a destination whose path is blocked by a plain file.
    def test_65_dry_run_flags_destination_blocked_by_file(self):
        self.write_raw(os.path.join(self.out, "blocker"), "not a directory")
        self.write_conf('$category = "report" => "$OUT/blocker/sub"')
        self.mkpair("a", "xml", '{"category":"report"}')
        self.dispatch("--dry-run")
        self.in_log("is not a directory")
        self.in_log("errors=1")

    # 66: --dry-run also checks the archive directory it does not create.
    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_66_dry_run_flags_unwritable_archive_dir(self):
        ro = os.path.join(self.sb, "ro-arch")
        os.makedirs(ro)
        os.chmod(ro, 0o555)
        self.archive = os.path.join(ro, "json")
        self.write_conf('$category = "report" => "$OUT/reports"')
        self.mkpair("a", "xml", '{"category":"report"}')
        self.dispatch("--dry-run")
        self.in_log("JSON_ARCHIVE_DIR")
        self.in_log("cannot create destination")

    # 67: a healthy destination stays silent (no false alarm on a missing dir).
    def test_67_dry_run_accepts_creatable_destination(self):
        self.write_conf('$category = "report" => "$OUT/deep/nested/new"')
        self.mkpair("a", "xml", '{"category":"report"}')
        self.dispatch("--dry-run")
        self.in_log("DRY-RUN SUCCESS move source=")
        self.in_log("errors=0")
        self.assertNotIn("FAILURE", self.log())
        self.absent(self.op("deep"))            # still creates nothing

    # 68: "+" concatenation in variables, destinations and conditions.
    def test_68_plus_concatenation(self):
        self.write_conf('$kind = "no" + "te" => $OUT + "/" + $DEEP',
                        extra='SUB = "a" + "/" + "b"\nDEEP = $SUB + "/" + upper($group)')
        self.mkpair("p", "xml", '{"kind":"note","group":"zz"}')
        self.dispatch()
        self.exists(self.op("a", "b", "ZZ", "p.xml"))

    # 69: a "+" inside quotes is literal text, not an operator.
    def test_69_quoted_plus_is_literal(self):
        self.write_conf('$kind = "c++" => "$OUT/" + "c++"')
        self.mkpair("p", "xml", '{"kind":"c++"}')
        self.dispatch()
        self.exists(self.op("c++", "p.xml"))

    # 70: a file still held open for writing is not moved, even when its size
    #     and mtime have stopped changing (the case the stability gate misses).
    def test_70_open_for_write_is_left_alone(self):
        self.write_conf('$category = "report" => "$OUT/r"')
        self.mkpair("busy", "xml", '{"category":"report"}')
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import time; f = open(%r, 'ab'); time.sleep(30)" % self.inc("busy.xml")])
        try:
            deadline = time.time() + 10        # let the holder get the fd open
            while time.time() < deadline and not self._holds_fd(holder.pid, self.inc("busy.xml")):
                time.sleep(0.05)
            self.dispatch()
        finally:
            holder.kill()
            holder.wait()
        self.exists(self.inc("busy.xml"))      # left in place
        self.no_files_under(self.out)
        self.in_log("open for writing")
        self.in_log("unstable=1")
        # ... and it goes through on the next run, once the writer is gone.
        self.dispatch()
        self.exists(self.op("r", "busy.xml"))

    def _holds_fd(self, pid, path):
        fddir = "/proc/%d/fd" % pid
        try:
            return any(os.readlink("%s/%s" % (fddir, fd)) == os.path.realpath(path)
                       for fd in os.listdir(fddir))
        except OSError:
            return False

    # 71: a field explicitly set to null passes REQUIRED and routes via ISNULL.
    def test_71_json_null_field(self):
        self.write_conf('$status ISNULL    => "$OUT/unset"\n'
                        '$status ISNOTNULL => "$OUT/set/" + lower($status)',
                        extra="REQUIRED = $category, $status")
        self.mkjson_obj("n", "csv", {"category": "alpha", "status": None,
                                     "kind": "report"})
        self.mkjson_obj("v", "csv", {"category": "alpha", "status": "Done"})
        self.mkjson_obj("e", "csv", {"category": "alpha", "status": ""})
        self.mkjson_obj("a", "csv", {"category": "alpha"})
        self.dispatch()
        self.exists(self.op("unset", "n.csv"))             # null -> accepted
        self.exists(self.op("set", "done", "v.csv"))
        self.exists(self.inc("e.csv"))                     # "" still rejected
        self.exists(self.inc("a.csv"))                     # absent still rejected
        self.assertEqual(self.errlog().count("missing/empty required field"), 2)

    # 72: conditional assignment (ternary with no else) chains like if/elif.
    def test_72_conditional_assignment_without_else(self):
        self.write_conf('$kind = "*" => "$OUT/" + $STAGE + "/" + $APP',
                        extra=('ST = upper($status)\n'
                               'APP = "misc"\n'
                               'APP = "reports"  if $ST CONTAINS "REPORT"\n'
                               'APP = "invoices" if $ST CONTAINS "INVOICE"\n'
                               'STAGE = "dev"\n'
                               'STAGE = "prod" if $env = "production"'))
        self.mkjson_obj("a", "xml", {"kind": "k", "status": "Report-9", "env": "production"})
        self.mkjson_obj("b", "xml", {"kind": "k", "status": "invoice-2", "env": "test"})
        self.mkjson_obj("c", "xml", {"kind": "k", "status": "other", "env": "test"})
        self.dispatch()
        self.exists(self.op("prod", "reports", "a.xml"))
        self.exists(self.op("dev", "invoices", "b.xml"))
        self.exists(self.op("dev", "misc", "c.xml"))    # no line matched -> default kept

    # 73: CREATE_DIRS defaults to no -- a missing destination is an error and
    #     the file stays put, rather than the tree being created underneath it.
    def test_73_missing_destination_is_an_error_by_default(self):
        with open(self.conf, "w") as f:
            f.write('INCOMING_DIR = "%s"\n' % self.incoming)
            f.write('JSON_ARCHIVE_DIR = "%s"\n' % self.archive)
            f.write('LOG_DIR = "%s"\n' % self.logdir)
            f.write("STABLE_SECONDS = 0\n")
            f.write('OUT = "%s"\n' % self.out)
            f.write('$category = "*" => "$OUT/$category"\n')
        self.mkpair("a", "xml", '{"category":"absent"}')
        self.dispatch()
        self.exists(self.inc("a.xml"))              # left in place
        self.absent(self.op("absent"))              # nothing created
        self.in_log("destination directory does not exist")
        self.in_log("errors=1")
        # ... and --dry-run says the same thing, in the same words.
        self.dispatch("--dry-run")
        self.in_log("DRY-RUN FAILURE move")

    # 74: an existing destination is used as-is when CREATE_DIRS is no.
    def test_74_existing_destination_works_without_create_dirs(self):
        os.makedirs(self.op("here"))
        with open(self.conf, "w") as f:
            f.write('INCOMING_DIR = "%s"\n' % self.incoming)
            f.write('JSON_ARCHIVE_DIR = "%s"\n' % self.archive)
            f.write('LOG_DIR = "%s"\n' % self.logdir)
            f.write("STABLE_SECONDS = 0\n")
            f.write("CREATE_DIRS = no\n")
            f.write('OUT = "%s"\n' % self.out)
            f.write('$category = "*" => "$OUT/here"\n')
        self.mkpair("a", "xml", '{"category":"x"}')
        self.dispatch()
        self.exists(self.op("here", "a.xml"))

    # 75: an unusable CREATE_DIRS value is refused by --check.
    def test_75_bad_create_dirs_value_is_refused(self):
        self.write_conf('$category = "*" => "$OUT/r"', extra="CREATE_DIRS = maybe")
        r = self.run_args(["--check", self.conf])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("CREATE_DIRS must be yes or no", r.stderr)

    # 76: a dry run and a real run log the same line for the same outcome --
    #     only the DRY-RUN prefix differs. That is what makes a dry run
    #     readable as a prediction of the real one.
    def test_76_dry_run_and_real_run_log_the_same_line(self):
        self.write_conf('$category = "*" => "$OUT/" + $category')
        self.mkpair("a", "xml", '{"category":"r"}')
        self.dispatch("--dry-run")
        dry = [l.split("] ", 1)[1] for l in self.log().splitlines() if "SUCCESS" in l]
        os.remove(self.logf)
        self.dispatch()
        real = [l.split("] ", 1)[1] for l in self.log().splitlines() if "SUCCESS" in l]
        self.assertEqual(len(dry), 1)
        self.assertEqual([l[len("DRY-RUN "):] for l in dry], real)

    # 77: system metadata is available to rules for a file that HAS a sidecar.
    def test_77_system_fields_alongside_a_sidecar(self):
        self.write_conf('$Filename ENDSWITH ".csv" => "$OUT/csv"\n'
                        '$Filesize > "100"         => "$OUT/big"\n'
                        '$category = "*"           => "$OUT/rest"')
        self.mkpair("a", "csv", '{"category":"c"}')
        self.mkpair("b", "bin", '{"category":"c"}', data="x" * 200)
        self.mkpair("c", "bin", '{"category":"c"}', data="x")
        self.dispatch()
        self.exists(self.op("csv", "a.csv"))
        self.exists(self.op("big", "b.bin"))
        self.exists(self.op("rest", "c.bin"))

    # 78: a sidecar field of the same name wins over the system one.
    def test_78_sidecar_overrides_a_system_field(self):
        self.write_conf('$Filename = "renamed" => "$OUT/from_json"\n'
                        '$Filename = "*"       => "$OUT/from_system"')
        self.mkpair("a", "xml", '{"Filename":"renamed"}')
        self.mkpair("b", "xml", '{"other":"x"}')
        self.dispatch()
        self.exists(self.op("from_json", "a.xml"))
        self.exists(self.op("from_system", "b.xml"))

    # 79: without DISPATCH_WITHOUT_JSON, a file with no sidecar still waits.
    def test_79_orphan_waits_by_default(self):
        self.write_conf('$Filename = "*" => "$OUT/any"')
        self.write_raw(self.inc("lonely.csv"), "DATA")
        self.dispatch()
        self.exists(self.inc("lonely.csv"))
        self.in_log("no .json sidecar yet")
        self.in_log("incomplete=1")
        self.no_files_under(self.out)

    # 80: with it on, the orphan is dispatched on its system metadata alone,
    #     and there is nothing to archive.
    def test_80_orphan_dispatched_on_system_metadata(self):
        self.write_conf('$Filename ENDSWITH ".csv" => "$OUT/csv"\n'
                        '$Filesize > "100"         => "$OUT/big"',
                        extra="DISPATCH_WITHOUT_JSON = yes")
        self.write_raw(self.inc("lonely.csv"), "DATA")
        self.write_raw(self.inc("heavy.bin"), "x" * 200)
        self.write_raw(self.inc("small.bin"), "x")
        self.dispatch()
        self.exists(self.op("csv", "lonely.csv"))
        self.exists(self.op("big", "heavy.bin"))
        self.exists(self.inc("small.bin"))          # matched nothing, stays put
        self.in_log("archived='-'")                 # no sidecar to archive
        self.no_files_under(self.archive)

    # 80b: a sidecar-less file that matches nothing names ITSELF in the log,
    #      not the sidecar it does not have.
    def test_80b_orphan_nomatch_names_the_data_file(self):
        self.write_conf('$Filename ENDSWITH ".csv" => "$OUT/csv"',
                        extra="DISPATCH_WITHOUT_JSON = yes")
        self.write_raw(self.inc("lonely.dat"), "DATA")
        self.dispatch()
        self.in_log("no rule matched source='%s'" % self.inc("lonely.dat"))
        self.assertNotIn("source='None'", self.log())
        self.exists(self.inc("lonely.dat"))

    # 81: a paired file is still paired when the setting is on -- the sidecar
    #     is read and archived exactly as before.
    def test_81_pairs_unaffected_by_the_setting(self):
        self.write_conf('$category = "r" => "$OUT/r"', extra="DISPATCH_WITHOUT_JSON = yes")
        self.mkpair("a", "xml", '{"category":"r"}')
        self.dispatch()
        self.exists(self.op("r", "a.xml"))
        self.exists(os.path.join(self.archive, "a.json"))

    # 81b: REQUIRED is a contract on the sidecar, so it is not held against a
    #      file that has none -- otherwise the two settings could not coexist.
    def test_81b_required_does_not_apply_without_a_sidecar(self):
        self.write_conf('$Filename ENDSWITH ".csv" => "$OUT/csv"',
                        extra="DISPATCH_WITHOUT_JSON = yes\nREQUIRED = $category")
        self.write_raw(self.inc("lonely.csv"), "DATA")          # no sidecar at all
        self.mkpair("ok", "csv", '{"category":"c"}')            # sidecar, field present
        self.mkpair("ko", "csv", '{"other":"x"}')               # sidecar, field missing
        self.dispatch()
        self.exists(self.op("csv", "lonely.csv"))               # dispatched
        self.exists(self.op("csv", "ok.csv"))
        self.exists(self.inc("ko.csv"))                         # still enforced here
        self.in_log("missing/empty required field(s): category")
        self.assertEqual(self.errlog().count("missing/empty required field"), 1)

    # 82: an unusable DISPATCH_WITHOUT_JSON value is refused by --check.
    def test_82_bad_dispatch_without_json_value(self):
        self.write_conf('$category = "*" => "$OUT/r"', extra="DISPATCH_WITHOUT_JSON = sometimes")
        r = self.run_args(["--check", self.conf])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DISPATCH_WITHOUT_JSON must be yes or no", r.stderr)

    # 83: errors.log holds real failures only -- no WARN, and nothing routine.
    def test_83_errors_log_holds_only_errors(self):
        self.write_conf('$category = "r" => "$OUT/r"', extra="STABLE_SECONDS = 0")
        self.mkpair("ok", "xml", '{"category":"r"}')            # success
        self.mkpair("none", "xml", '{"category":"other"}')      # no rule matched: WARN
        self.mkpair("bad", "xml", "not json at all")            # invalid: ERROR
        self.dispatch()
        err = self.errlog()
        self.assertIn("invalid JSON", err)                      # the real failure
        self.assertNotIn("[WARN]", err)
        self.assertNotIn("no rule matched", err)                # routine, not an error
        self.assertNotIn("SUCCESS", err)
        # dispatch.log still has all of it.
        self.in_log("no rule matched")
        self.in_log("invalid JSON")
        self.in_log("SUCCESS move")

    # 84: a skipped symlink is a failure, so it reaches errors.log too --
    #     it was already counted in errors= while being logged as a warning.
    def test_84_symlink_is_logged_as_an_error(self):
        self.write_conf('$category = "r" => "$OUT/r"')
        self.mkpair("a", "xml", '{"category":"r"}')
        os.remove(self.inc("a.xml"))
        os.symlink("/etc/hostname", self.inc("a.xml"))
        self.dispatch()
        self.assertIn("data file is a symlink", self.errlog())
        self.in_log("errors=1")

    # 85: DRY_RUN and DEBUG can be set in the config ...
    def test_85_dry_run_and_debug_from_the_config(self):
        self.write_conf('$category = "r" => "$OUT/r"', extra="DRY_RUN = yes\nDEBUG = yes")
        self.mkpair("a", "xml", '{"category":"r"}')
        self.dispatch()
        self.exists(self.inc("a.xml"))              # DRY_RUN = yes: nothing moved
        self.no_files_under(self.out)
        self.in_log("DRY-RUN SUCCESS move")
        self.in_log("[DEBUG]")                      # DEBUG = yes: trace present

    # 86: ... and the command line overrides them, in both directions.
    def test_86_command_line_beats_the_config(self):
        self.write_conf('$category = "r" => "$OUT/r"', extra="DRY_RUN = yes\nDEBUG = yes")
        self.mkpair("a", "xml", '{"category":"r"}')
        self.dispatch("--no-dry-run", "--no-debug")
        self.exists(self.op("r", "a.xml"))          # moved despite DRY_RUN = yes
        self.assertNotIn("[DEBUG]", self.log())
        self.assertNotIn("DRY-RUN", self.log())

    def test_86b_flags_win_over_a_config_that_says_no(self):
        self.write_conf('$category = "r" => "$OUT/r"', extra="DRY_RUN = no\nDEBUG = no")
        self.mkpair("a", "xml", '{"category":"r"}')
        self.dispatch("--dry-run", "--debug")
        self.exists(self.inc("a.xml"))
        self.in_log("DRY-RUN")
        self.in_log("[DEBUG]")

    # 87: an unusable value is refused by --check, like the other yes/no settings.
    def test_87_bad_dry_run_value_is_refused(self):
        self.write_conf('$category = "*" => "$OUT/r"', extra="DRY_RUN = perhaps")
        r = self.run_args(["--check", self.conf])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DRY_RUN must be yes or no", r.stderr)

    # 88: a failed move names the system's reason, not just "move failed".
    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_88_move_failure_reports_the_cause(self):
        dest = self.op("locked")
        os.makedirs(dest)
        os.chmod(dest, 0o555)
        self.write_conf('$category = "*" => "$OUT/locked"')
        self.mkpair("a", "xml", '{"category":"x"}')
        self.dispatch()
        self.in_log("reason='move failed'")
        # the pre-check names the problem before anything is attempted
        self.in_log("step='check'")
        self.in_log("cause='[Errno 13] destination directory is not writable'")
        self.exists(self.inc("a.xml"))

    # 89: a destination that cannot be created says why.
    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_89_create_failure_reports_the_cause(self):
        ro = os.path.join(self.sb, "ro")
        os.makedirs(ro)
        os.chmod(ro, 0o555)
        self.write_conf('$category = "*" => "%s/sub"' % ro)
        self.mkpair("a", "xml", '{"category":"x"}')
        self.dispatch()
        self.in_log("reason='cannot create destination'")
        self.in_log("cause='[Errno 13] Permission denied'")

    # 90: invalid JSON says what is wrong with it, and where.
    def test_90_invalid_json_reports_the_cause(self):
        self.write_conf('$category = "*" => "$OUT/r"')
        self.mkpair("broken", "xml", '{"category": "x",,}')
        self.mkpair("array", "xml", "[1, 2, 3]")
        self.dispatch()
        err = self.errlog()
        self.assertIn("cause='Expecting property name", err)
        self.assertIn("cause='top level is list, expected an object'", err)

    # 91: a failed move is followed by everything needed to act on it.
    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_91_move_failure_emits_diagnostics(self):
        dest = self.op("locked")
        os.makedirs(dest)
        os.chmod(dest, 0o555)
        self.write_conf('$category = "*" => "$OUT/locked"')
        self.mkpair("a", "xml", '{"category":"x"}')
        self.dispatch()
        diag = [l for l in self.errlog().splitlines() if "DIAG move" in l]
        self.assertEqual(len(diag), 1, self.errlog())
        line = diag[0]
        self.assertIn("failed_on='%s" % dest, line)     # the path the kernel refused
        self.assertIn("mode=0555", line)                # why it refused
        self.assertIn("ours=rx", line)                  # what we can actually do
        self.assertIn("same_filesystem=yes", line)
        self.assertIn("process=", line)                 # who we are
        self.assertIn("uid=%d" % os.getuid(), line)
        self.assertIn("umask=", line)
        self.assertIn("needs write+execute on it", line)

    # 92: the diagnostic reports the closest existing ancestor when the
    #     destination itself could not be created.
    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_92_create_failure_emits_diagnostics(self):
        ro = os.path.join(self.sb, "ro")
        os.makedirs(ro)
        os.chmod(ro, 0o555)
        self.write_conf('$category = "*" => "%s/a/b/c"' % ro)
        self.mkpair("a", "xml", '{"category":"x"}')
        self.dispatch()
        diag = [l for l in self.errlog().splitlines() if "DIAG create" in l]
        self.assertEqual(len(diag), 1, self.errlog())
        self.assertIn("dest_dir='%s'" % ro, diag[0])    # the ancestor that decides
        self.assertIn("mode=0555", diag[0])

    # 93: a run with nothing wrong emits no diagnostics at all.
    def test_93_no_diagnostics_on_a_clean_run(self):
        self.write_conf('$category = "*" => "$OUT/r"')
        self.mkpair("a", "xml", '{"category":"x"}')
        self.dispatch()
        self.assertNotIn("DIAG", self.log())

    # 94: a move across filesystems copies, verifies and publishes atomically --
    #     the content is intact and no partial file is left behind.
    @unittest.skipUnless(os.path.isdir("/dev/shm"), "needs a second filesystem")
    def test_94_cross_filesystem_move(self):
        dest = tempfile.mkdtemp(prefix="fd-xfs-", dir="/dev/shm")
        self.addCleanup(shutil.rmtree, dest, True)
        self.write_conf('$category = "*" => "%s"' % dest)
        payload = os.urandom(300000)
        with open(self.inc("big.bin"), "wb") as f:
            f.write(payload)
        self.write_raw(self.inc("big.json"), '{"category":"x"}')
        self.dispatch()
        with open(os.path.join(dest, "big.bin"), "rb") as f:
            self.assertEqual(f.read(), payload)             # byte for byte
        self.assertEqual([f for f in os.listdir(dest) if "partial" in f], [])
        self.absent(self.inc("big.bin"))                    # source removed last
        self.in_log("SUCCESS move")

    # 95: delivered but the source survived -- the one case that would dispatch
    #     the same file twice, so it must be unmistakable in the log.
    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_95_copied_but_source_not_removed(self):
        self.write_conf('$category = "*" => "$OUT/r"')
        self.mkpair("a", "xml", '{"category":"x"}')
        os.makedirs(self.op("r"))
        os.chmod(self.incoming, 0o555)                      # can read, cannot unlink
        self.dispatch()
        os.chmod(self.incoming, 0o755)
        self.exists(self.op("r", "a.xml"))                  # delivered
        self.exists(self.inc("a.xml"))                      # and still here
        self.in_log("will be dispatched again next run")
        self.in_log("step='remove_source'")
        self.in_log("errors=1")

    # 96: the pre-checks refuse before touching anything, and name what is wrong.
    def test_96_prechecks_name_the_problem(self):
        self.write_conf('$category = "*" => "$OUT/nope"')   # CREATE_DIRS is on here
        self.mkpair("a", "xml", '{"category":"x"}')
        os.makedirs(self.op("nope"))
        os.chmod(self.op("nope"), 0o555)     # tearDown restores it before cleanup
        self.dispatch()
        self.in_log("step='check'")
        self.in_log("destination directory is not writable")
        self.no_files_under(self.op("nope"))

    # 97: one row per file, updated in place across runs, closed by a success.
    def test_97_report_tracks_a_file_until_it_succeeds(self):
        rep = os.path.join(self.sb, "report")
        self.write_conf('$category = "ok" => "$OUT/ok"\n$category = "wait" => "$OUT/wait"',
                        extra='REPORT_DIR = "%s"\nCREATE_DIRS = no' % rep)
        os.makedirs(self.op("ok"))
        self.mkpair("good", "csv", '{"category":"ok"}')
        self.mkpair("late", "csv", '{"category":"wait"}')       # destination missing
        self.mkpair("odd", "csv", '{"category":"other"}')       # no rule
        self.dispatch()
        self.dispatch()                                          # nothing changes
        os.makedirs(self.op("wait"))                             # now it can go
        self.dispatch()

        rows = {r["filename"]: r for r in self.read_report(rep)}
        self.assertEqual(sorted(rows), ["good.csv", "late.csv", "odd.csv"])
        self.assertEqual(rows["good.csv"]["status"], "success")
        self.assertEqual(rows["good.csv"]["retries"], "0")
        self.assertTrue(rows["good.csv"]["moved_at"])
        self.assertTrue(rows["good.csv"]["file_date"])           # read before the move
        self.assertEqual(rows["good.csv"]["destination"], self.op("ok"))
        # failed twice, then succeeded -- still ONE row
        self.assertEqual(rows["late.csv"]["status"], "success")
        self.assertEqual(rows["late.csv"]["retries"], "2")
        self.assertEqual(rows["odd.csv"]["status"], "unmatched")
        self.assertEqual(rows["odd.csv"]["reason"], "no rule matched")

    # 98: a name that comes back after a success is a NEW file, not the old row.
    def test_98_a_reused_name_opens_a_new_row(self):
        rep = os.path.join(self.sb, "report")
        self.write_conf('$category = "*" => "$OUT/r"', extra='REPORT_DIR = "%s"' % rep)
        self.mkpair("export", "csv", '{"category":"x"}')
        self.dispatch()
        self.mkpair("export", "csv", '{"category":"x"}')          # same name, later
        self.dispatch()
        rows = [r for r in self.read_report(rep) if r["filename"] == "export.csv"]
        self.assertEqual(len(rows), 2, rows)                      # two files, two rows
        self.assertEqual([r["status"] for r in rows], ["success", "success"])
        self.assertNotEqual(rows[0]["first_seen"], rows[1]["first_seen"])

    # 99: a failure is not repeated -- one row, a growing retry count, a reason.
    def test_99_failure_is_one_row_with_retries(self):
        rep = os.path.join(self.sb, "report")
        self.write_conf('$category = "*" => "$OUT/missing"',
                        extra='REPORT_DIR = "%s"\nCREATE_DIRS = no' % rep)
        self.mkpair("a", "csv", '{"category":"x"}')
        for _ in range(3):
            self.dispatch()
        rows = self.read_report(rep)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(rows[0]["retries"], "2")                 # 1 attempt + 2 retries
        self.assertIn("destination directory does not exist", rows[0]["reason"])
        self.assertEqual(rows[0]["destination"], self.op("missing"))
        self.assertEqual(rows[0]["moved_at"], "")

    # 100: no REPORT_DIR, no report; and a dry run never writes one.
    def test_100_report_is_opt_in_and_dry_run_writes_nothing(self):
        rep = os.path.join(self.sb, "report")
        self.write_conf('$category = "*" => "$OUT/r"')
        self.mkpair("a", "csv", '{"category":"x"}')
        self.dispatch()
        self.assertFalse(os.path.exists(rep))
        self.write_conf('$category = "*" => "$OUT/r"', extra='REPORT_DIR = "%s"' % rep)
        self.dispatch("--dry-run")
        self.assertFalse(os.path.exists(os.path.join(rep, "report.csv")))

    # 101: a locked report.csv costs nothing -- the state keeps the run's work
    #      and the published copy catches up once the lock is gone.
    def test_101_locked_report_csv_loses_nothing(self):
        rep = os.path.join(self.sb, "report")
        self.write_conf('$category = "*" => "$OUT/r"', extra='REPORT_DIR = "%s"' % rep)
        self.mkpair("one", "csv", '{"category":"x"}')
        self.dispatch()
        self.assertEqual(len(self.read_report(rep)), 1)

        # Stand in for a spreadsheet holding the file: os.replace onto this
        # path cannot work, while every other write in the directory still can.
        os.remove(os.path.join(rep, "report.csv"))
        os.mkdir(os.path.join(rep, "report.csv"))
        self.mkpair("two", "csv", '{"category":"x"}')
        self.dispatch()
        self.in_log("report.csv could not be updated")
        self.assertIn("[WARN]", self.log())
        self.assertNotIn("could not be updated", self.errlog())   # transient, self-healing
        with open(os.path.join(rep, "report.state"), newline="") as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), 2)    # kept anyway

        os.rmdir(os.path.join(rep, "report.csv"))                 # lock released
        self.mkpair("three", "csv", '{"category":"x"}')
        self.dispatch()
        names = [r["filename"] for r in self.read_report(rep)]
        self.assertEqual(sorted(names), ["one.csv", "three.csv", "two.csv"])

    # 102: an existing report.csv is adopted when there is no state yet.
    def test_102_existing_report_is_adopted(self):
        rep = os.path.join(self.sb, "report")
        self.write_conf('$category = "*" => "$OUT/r"', extra='REPORT_DIR = "%s"' % rep)
        self.mkpair("old", "csv", '{"category":"x"}')
        self.dispatch()
        os.remove(os.path.join(rep, "report.state"))              # as if upgrading
        self.mkpair("new", "csv", '{"category":"x"}')
        self.dispatch()
        names = [r["filename"] for r in self.read_report(rep)]
        self.assertEqual(sorted(names), ["new.csv", "old.csv"])   # history carried over

    # 103: REPORT_SPLIT partitions rows into one file per period, and leaves a
    #      past period alone -- so a spreadsheet open on it never collides.
    def test_103_report_split_partitions_and_leaves_the_past_alone(self):
        rep = os.path.join(self.sb, "report")
        self.write_conf('$category = "*" => "$OUT/r"',
                        extra='REPORT_DIR = "%s"\nREPORT_SPLIT = monthly' % rep)
        self.mkpair("old", "csv", '{"category":"x"}')
        self.dispatch()
        self.backdate(rep, "old.csv", "2026-07-15T09:00:00.000")

        self.mkpair("new", "csv", '{"category":"x"}')
        self.dispatch()
        published = sorted(f for f in os.listdir(rep) if f.endswith(".csv"))
        self.assertEqual(len(published), 2, published)
        self.assertTrue(any(f.endswith("-2026-07.csv") for f in published), published)
        # each row lives in exactly ONE file: no duplicates to reconcile
        rows = []
        for f in published:
            with open(os.path.join(rep, f), newline="") as fh:
                rows += list(csv.DictReader(fh))
        self.assertEqual(sorted(r["filename"] for r in rows), ["new.csv", "old.csv"])

        # July is untouchable from now on; a run must not even try
        july = os.path.join(rep, [f for f in published if "2026-07" in f][0])
        os.remove(july)
        os.mkdir(july)
        self.mkpair("third", "csv", '{"category":"x"}')
        self.dispatch()
        self.assertNotIn("could not be updated", self.log())
        self.assertTrue(os.path.isdir(july))            # never written to

    # 104: an unusable REPORT_SPLIT is refused by --check.
    def test_104_bad_report_split_is_refused(self):
        self.write_conf('$category = "*" => "$OUT/r"', extra="REPORT_SPLIT = weekly")
        r = self.run_args(["--check", self.conf])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REPORT_SPLIT must be one of", r.stderr)

    # 105: a run with nothing to do rewrites nothing -- on a network share the
    #      report is real traffic, and a quiet cron runs far more often than
    #      files arrive.
    def test_105_idle_run_touches_no_report_file(self):
        rep = os.path.join(self.sb, "report")
        self.write_conf('$category = "*" => "$OUT/r"', extra='REPORT_DIR = "%s"' % rep)
        self.mkpair("a", "csv", '{"category":"x"}')
        self.dispatch()
        before = {f: os.stat(os.path.join(rep, f)).st_mtime_ns for f in os.listdir(rep)}
        for _ in range(3):
            self.dispatch()                                  # incoming is empty now
        after = {f: os.stat(os.path.join(rep, f)).st_mtime_ns for f in os.listdir(rep)}
        self.assertEqual(before, after)
        # ... and a new file still wakes it up
        self.mkpair("b", "csv", '{"category":"x"}')
        self.dispatch()
        self.assertEqual(len(self.read_report(rep)), 2)

    # 106: a publish that failed is republished on the next run, even a quiet
    # one. Without this the report stays stale until a new file happens to
    # arrive, which on a quiet weekend is days.
    def test_106_stale_report_is_republished_on_a_quiet_run(self):
        rep = os.path.join(self.sb, "report")
        self.write_conf('$category = "*" => "$OUT/r"', extra='REPORT_DIR = "%s"' % rep)
        self.mkpair("one", "csv", '{"category":"x"}')
        self.dispatch()

        # A spreadsheet holding the file: the publish fails, the state does not.
        os.remove(os.path.join(rep, "report.csv"))
        os.mkdir(os.path.join(rep, "report.csv"))
        self.mkpair("two", "csv", '{"category":"x"}')
        self.dispatch()
        self.in_log("report.csv could not be updated")

        # Lock released, and nothing new to process: it must heal by itself.
        os.rmdir(os.path.join(rep, "report.csv"))
        self.dispatch()
        names = [r["filename"] for r in self.read_report(rep)]
        self.assertEqual(sorted(names), ["one.csv", "two.csv"])

        # ...and the idle shortcut still holds once it is back in step.
        before = {f: os.stat(os.path.join(rep, f)).st_mtime_ns for f in os.listdir(rep)}
        for _ in range(3):
            self.dispatch()
        after = {f: os.stat(os.path.join(rep, f)).st_mtime_ns for f in os.listdir(rep)}
        self.assertEqual(before, after, "a quiet run must not rewrite anything")

    # 109: the report names where the archived copy of the data went.
    def test_109_report_carries_the_data_archive_path(self):
        rep = os.path.join(self.sb, "report")
        darch = os.path.join(self.sb, "darch")
        self.write_conf('$category = "ok" => "$OUT/r"',
                        extra='REPORT_DIR = "%s"\nDATA_ARCHIVE_DIR = "%s"' % (rep, darch))
        self.mkpair("kept", "csv", '{"category":"ok"}')
        self.mkpair("nope", "csv", '{"category":"other"}')       # never delivered
        self.dispatch()
        rows = {r["filename"]: r for r in self.read_report(rep)}
        self.assertEqual(rows["kept.csv"]["data_archive"], os.path.join(darch, "kept.csv"))
        self.assertEqual(rows["nope.csv"]["data_archive"], "")   # nothing archived

    # 109b: and where the sidecar was archived -- empty when there was none.
    def test_109b_report_carries_the_json_archive_path(self):
        rep = os.path.join(self.sb, "report")
        self.write_conf('$Filename ENDSWITH ".csv" => "$OUT/r"',
                        extra='REPORT_DIR = "%s"\nDISPATCH_WITHOUT_JSON = yes' % rep)
        self.mkpair("paired", "csv", '{"category":"x"}')
        self.write_raw(self.inc("lonely.csv"), "DATA")           # no sidecar at all
        self.dispatch()
        rows = {r["filename"]: r for r in self.read_report(rep)}
        self.assertEqual(rows["paired.csv"]["json_archive"],
                         os.path.join(self.archive, "paired.json"))
        self.assertEqual(rows["lonely.csv"]["json_archive"], "")

    # 110: a report written before the column existed still loads.
    def test_110_report_without_the_column_still_loads(self):
        rep = os.path.join(self.sb, "report")
        self.write_conf('$category = "*" => "$OUT/r"', extra='REPORT_DIR = "%s"' % rep)
        self.mkpair("old", "csv", '{"category":"x"}')
        self.dispatch()
        state = os.path.join(rep, "report.state")
        with open(state, newline="") as fh:
            rows = list(csv.DictReader(fh))
        older = [c for c in rows[0]
                 if c not in ("data_archive", "json_archive")]    # as an older version wrote it
        with open(state, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=older, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        self.mkpair("new", "csv", '{"category":"x"}')
        self.dispatch()
        rows = {r["filename"]: r for r in self.read_report(rep)}
        self.assertEqual(sorted(rows), ["new.csv", "old.csv"])   # history kept
        self.assertEqual(rows["old.csv"]["data_archive"], "")    # simply empty
        self.assertEqual(rows["old.csv"]["json_archive"], "")

    def backdate(self, rep, filename, when):
        """Rewrite one row's first_seen, to stand in for an earlier period."""
        path = os.path.join(rep, "report.state")
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            if row["filename"] == filename:
                row["first_seen"] = when
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    def read_report(self, rep):
        with open(os.path.join(rep, "report.csv"), newline="") as fh:
            return list(csv.DictReader(fh))

    # 106: DATA_ARCHIVE_DIR keeps a copy of what was delivered, and the three
    #      copies of one delivery share a name.
    def test_106_data_archive_keeps_a_copy(self):
        darch = os.path.join(self.sb, "darch")
        self.write_conf('$category = "*" => "$OUT/r"',
                        extra='DATA_ARCHIVE_DIR = "%s"' % darch)
        self.mkpair("a", "csv", '{"category":"x"}', data="PAYLOAD")
        self.dispatch()
        self.exists(self.op("r", "a.csv"))                       # delivered
        with open(os.path.join(darch, "a.csv")) as fh:
            self.assertEqual(fh.read(), "PAYLOAD")               # and archived
        self.exists(os.path.join(self.archive, "a.json"))
        self.in_log("data_archived='%s'" % os.path.join(darch, "a.csv"))

    def test_106b_the_three_copies_share_one_collision_suffix(self):
        darch = os.path.join(self.sb, "darch")
        self.write_conf('$category = "*" => "$OUT/r"',
                        extra='DATA_ARCHIVE_DIR = "%s"' % darch)
        for _ in range(2):                                       # same name twice
            self.mkpair("lot", "csv", '{"category":"x"}')
            self.dispatch()
        suffixed = [f for f in os.listdir(self.op("r")) if f != "lot.csv"]
        self.assertEqual(len(suffixed), 1, suffixed)
        stamp = suffixed[0][len("lot.csv."):]
        self.exists(os.path.join(darch, "lot.csv." + stamp))
        self.exists(os.path.join(self.archive, "lot.json." + stamp))

    # 107: archiving is secondary -- a failure there is reported, but what was
    #      delivered stays delivered and is not sent again.
    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_107_failed_data_archive_does_not_undo_the_delivery(self):
        darch = os.path.join(self.sb, "darch")
        os.makedirs(darch)
        os.chmod(darch, 0o555)
        self.write_conf('$category = "*" => "$OUT/r"',
                        extra='DATA_ARCHIVE_DIR = "%s"' % darch)
        self.mkpair("a", "csv", '{"category":"x"}')
        self.dispatch()
        self.exists(self.op("r", "a.csv"))                       # delivered anyway
        self.absent(self.inc("a.csv"))                           # not retried
        self.assertIn("archiving a copy failed", self.errlog())
        self.in_log("errors=1")

    # 108: without the setting, nothing changes -- data only at its destination.
    def test_108_data_archive_is_opt_in(self):
        self.write_conf('$category = "*" => "$OUT/r"')
        self.mkpair("a", "csv", '{"category":"x"}')
        self.dispatch()
        self.exists(self.op("r", "a.csv"))
        self.assertNotIn("data_archived", self.log())


class TestLogRotation(unittest.TestCase):
    """The rotation itself, driven directly: the cap is in whole megabytes, so
    exercising the shifting through a real run would mean writing tens of MB."""

    def setUp(self):
        sys.path.insert(0, ROOT)
        import dispatch                                   # noqa: E402
        self.d = dispatch
        self.sb = tempfile.mkdtemp(prefix="fd-rot-")
        self.log = os.path.join(self.sb, "dispatch.log")
        self._saved = (dispatch.LOG_MAX_BYTES, dispatch.LOG_KEEP)

    def tearDown(self):
        self.d.LOG_MAX_BYTES, self.d.LOG_KEEP = self._saved
        shutil.rmtree(self.sb, ignore_errors=True)

    def write(self, text):
        with open(self.log, "a") as f:
            f.write(text + "\n")

    def gen(self, i):
        return os.path.join(self.sb, "dispatch.log.%d" % i)

    def test_rotation_shifts_and_drops_the_oldest(self):
        self.d.LOG_MAX_BYTES, self.d.LOG_KEEP = 100, 3
        for i in range(1, 8):
            self.d._append(self.log, "line %d %s" % (i, "x" * 90))
        # dispatch.log plus exactly LOG_KEEP generations, nothing beyond
        self.assertTrue(os.path.exists(self.log))
        for i in (1, 2, 3):
            self.assertTrue(os.path.exists(self.gen(i)), "missing .%d" % i)
        self.assertFalse(os.path.exists(self.gen(4)))
        # the newest content is in the unsuffixed file, the oldest survivor in .3
        self.assertIn("line 7", open(self.log).read())
        self.assertIn("line 4", open(self.gen(3)).read())

    def test_zero_disables_rotation(self):
        self.d.LOG_MAX_BYTES, self.d.LOG_KEEP = 0, 3
        for i in range(20):
            self.d._append(self.log, "x" * 200)
        self.assertFalse(os.path.exists(self.gen(1)))
        self.assertGreater(os.path.getsize(self.log), 200)

    def test_keep_zero_truncates_instead_of_keeping_generations(self):
        self.d.LOG_MAX_BYTES, self.d.LOG_KEEP = 100, 0
        for i in range(10):
            self.d._append(self.log, "line %d %s" % (i, "x" * 90))
        self.assertFalse(os.path.exists(self.gen(1)))
        self.assertLessEqual(os.path.getsize(self.log), 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
