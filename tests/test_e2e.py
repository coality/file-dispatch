#!/usr/bin/env python3
"""End-to-end tests for file-dispatch.

Each test builds an isolated sandbox, writes a dispatch.conf, drops files in the
incoming directory, runs the real entry point (./dispatch.sh) as a subprocess,
and asserts the final state: file locations, log contents, and exit code.
"""

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

    # 82: an unusable DISPATCH_WITHOUT_JSON value is refused by --check.
    def test_82_bad_dispatch_without_json_value(self):
        self.write_conf('$category = "*" => "$OUT/r"', extra="DISPATCH_WITHOUT_JSON = sometimes")
        r = self.run_args(["--check", self.conf])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DISPATCH_WITHOUT_JSON must be yes or no", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
