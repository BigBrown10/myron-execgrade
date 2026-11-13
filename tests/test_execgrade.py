"""Unit tests for execgrade. Run: python -m unittest discover -s tests -v"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from execgrade import sandbox  # noqa: E402
from execgrade.patch import extract_patch, files_in_patch, reverse_patch, touches_tests  # noqa: E402
from execgrade.tasks import Task, is_swesmith, parse_tests, swesmith_image, to_task  # noqa: E402

BUG = """diff --git a/pkg/mod.py b/pkg/mod.py
index af7cdf5..89a2b26 100644
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -2,7 +2,4 @@ def has_frame(x):
     if x is None:
         return False
-    if x.find("rich"):
-        return True
-    return False
-
+    return x.has_rich
 def other():
"""
CLEAN = 'def has_frame(x):\n    if x is None:\n        return False\n    if x.find("rich"):\n' \
        '        return True\n    return False\n\ndef other():\n    return 1\n'


def git(cwd, *args, stdin=None):
    # bytes on stdin: text mode would rewrite line endings on Windows and break the patch
    r = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd,
                       input=stdin.encode("utf-8") if stdin is not None else None,
                       capture_output=True)
    r.stderr = r.stderr.decode("utf-8", "replace")
    return r


class ReversePatch(unittest.TestCase):
    def test_roundtrip_with_real_git(self):
        """clean --bug--> buggy --reverse(bug)--> clean, checked by git itself."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            git(d, "init", "-q")
            (d / "pkg").mkdir()
            (d / "pkg" / "mod.py").write_text(CLEAN, encoding="utf-8", newline="\n")
            r = git(d, "apply", "-", stdin=BUG)
            self.assertEqual(r.returncode, 0, r.stderr)
            buggy = (d / "pkg" / "mod.py").read_text(encoding="utf-8")
            self.assertIn("has_rich", buggy)
            r = git(d, "apply", "-", stdin=reverse_patch(BUG))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual((d / "pkg" / "mod.py").read_text(encoding="utf-8"), CLEAN)

    def test_double_reverse_is_identity_on_content(self):
        twice = reverse_patch(reverse_patch(BUG))
        self.assertEqual(twice.replace("\n \n", "\n\n"), BUG.replace("\n \n", "\n\n"))

    def test_new_file_becomes_deleted_file(self):
        p = ("diff --git a/n.py b/n.py\nnew file mode 100644\nindex 0000000..f70ff53\n"
             "--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,1 @@\n+x = 1\n")
        r = reverse_patch(p)
        self.assertIn("deleted file mode 100644", r)
        self.assertIn("--- a/n.py\n+++ /dev/null", r)
        self.assertIn("@@ -1,1 +0,0 @@\n-x = 1", r)

    def test_no_newline_marker_stays_with_its_line(self):
        """Found on real data: 22 exam patches carry this marker and broke the first version."""
        p = ("diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n a = 1\n"
             "-b = 2\n\\ No newline at end of file\n+b = 3\n")
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            git(d, "init", "-q")
            (d / "f.py").write_bytes(b"a = 1\nb = 2")            # no trailing newline
            self.assertEqual(git(d, "apply", "-", stdin=p).returncode, 0)
            self.assertEqual((d / "f.py").read_bytes(), b"a = 1\nb = 3\n")
            r = git(d, "apply", "-", stdin=reverse_patch(p))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual((d / "f.py").read_bytes(), b"a = 1\nb = 2")

    def test_minus_lines_come_first(self):
        r = reverse_patch(BUG)
        body = [l for l in r.split("\n") if l[:1] in ("+", "-") and not l.startswith(("---", "+++"))]
        self.assertEqual(body[0], "-    return x.has_rich")
        self.assertTrue(all(l.startswith("+") for l in body[1:]))


class PatchHelpers(unittest.TestCase):
    def test_files_in_patch(self):
        self.assertEqual(files_in_patch(BUG), ["pkg/mod.py"])

    def test_touches_tests(self):
        self.assertFalse(touches_tests(BUG))
        self.assertTrue(touches_tests(BUG.replace("pkg/mod.py", "tests/test_mod.py")))
        self.assertTrue(touches_tests(BUG.replace("pkg/mod.py", "pkg/conftest.py")))

    def test_extract_from_fence_and_prose(self):
        raw = "Here is the fix:\n```diff\n" + BUG + "```\nHope this helps!"
        self.assertEqual(extract_patch(raw), BUG)
        raw2 = "FIX: File(s): pkg/mod.py\n" + BUG + "\nThat should do it."
        self.assertEqual(extract_patch(raw2), BUG)

    def test_extract_nothing(self):
        self.assertEqual(extract_patch("170:         else:\n171:   await x()"), "")
        self.assertEqual(extract_patch(""), "")


class Tasks(unittest.TestCase):
    def test_parse_tests_handles_the_string_repr_bug(self):
        s = "['tests/a.py::test_x', 'tests/a.py::T::test_y[p1]']"
        self.assertEqual(parse_tests(s), ["tests/a.py::test_x", "tests/a.py::T::test_y[p1]"])
        self.assertEqual(parse_tests(["a::b"]), ["a::b"])
        self.assertEqual(parse_tests(""), [])
        # what the old prompt builder did with the same field:
        self.assertEqual(", ".join(s[:3]), "[, ', t")

    def test_swesmith_row_is_reversed(self):
        row = {"id": "o__r.abc.func_x", "repo": "swesmith/o__r.abc", "patch": BUG,
               "problem_statement": "p", "fail_tests": "['tests/t.py::a']"}
        self.assertTrue(is_swesmith(row))
        t = to_task(row, fetch_meta=False)
        self.assertEqual(t.family, "swesmith")
        self.assertEqual(t.bug_patch, BUG)
        self.assertIn("-    return x.has_rich", t.fix_patch)   # the fix REMOVES the bug line
        self.assertEqual(t.image, "swebench/swesmith.x86_64.o_1776_r.abc")
        self.assertTrue(t.runnable)

    def test_real_row_is_not_reversed_and_not_runnable(self):
        row = {"id": "getmoto__moto-1", "repo": "getmoto/moto", "patch": BUG,
               "problem_statement": "p", "fail_tests": "['tests/t.py::a']"}
        t = to_task(row)
        self.assertEqual((t.family, t.fix_patch, t.runnable), ("real", BUG, False))

    def test_image_name(self):
        self.assertEqual(swesmith_image("swesmith/scanny__python-pptx.278b47b1"),
                         "swebench/swesmith.x86_64.scanny_1776_python-pptx.278b47b1")


LOG = """\
....F.                                                                   [100%]
==================================== PASSES ====================================
=========================== short test summary info ============================
PASSED tests/t.py::T::test_a[p1]
PASSED tests/t.py::test_b
FAILED tests/t.py::test_c - AttributeError: 'X' object has no attribute 'y'
ERROR tests/t.py::test_d - ImportError: nope
SKIPPED [1] tests/t.py:9: reason
1 failed, 2 passed, 1 error in 0.31s
"""


def _task(f2p):
    return Task(id="x", repo="swesmith/o__r.abc", family="swesmith", issue="", bug_patch=BUG,
                fix_patch=reverse_patch(BUG), fail_to_pass=f2p, image="img")


def _run(statuses, apply="git"):
    return sandbox.Run(ok=True, bug_apply="OK", cand_apply=apply, statuses=statuses)


class Verdicts(unittest.TestCase):
    def test_parse_statuses(self):
        st = sandbox.parse_statuses(LOG)
        self.assertEqual(st["tests/t.py::T::test_a[p1]"], "PASSED")
        self.assertEqual(st["tests/t.py::test_c"], "FAILED")
        self.assertEqual(st["tests/t.py::test_d"], "ERROR")

    def test_test_files(self):
        self.assertEqual(sandbox.test_files(["a/t.py::x", "a/t.py::y[1]", "b/u.py::z"]),
                         ["a/t.py", "b/u.py"])

    def test_resolved(self):
        t = _task(["t::c"])
        buggy = _run({"t::a": "PASSED", "t::c": "FAILED"}, apply="")
        good = _run({"t::a": "PASSED", "t::c": "PASSED"})
        self.assertTrue(sandbox.verdict(t, buggy, good, BUG)["resolved"])

    def test_regression_blocks_resolve(self):
        t = _task(["t::c"])
        buggy = _run({"t::a": "PASSED", "t::c": "FAILED"}, apply="")
        broke = _run({"t::a": "FAILED", "t::c": "PASSED"})
        v = sandbox.verdict(t, buggy, broke, BUG)
        self.assertEqual((v["resolved"], v["regressions"]), (False, 1))

    def test_partial_f2p_is_not_resolved(self):
        t = _task(["t::c", "t::d"])
        buggy = _run({"t::c": "FAILED", "t::d": "FAILED"}, apply="")
        half = _run({"t::c": "PASSED", "t::d": "FAILED"})
        v = sandbox.verdict(t, buggy, half, BUG)
        self.assertEqual((v["resolved"], v["f2p_passed"]), (False, 1))

    def test_missing_test_is_not_a_pass(self):
        """The logic-inversion lesson: a test that vanished from the log is NOT green."""
        t = _task(["t::c"])
        buggy = _run({"t::c": "FAILED"}, apply="")
        self.assertFalse(sandbox.verdict(t, buggy, _run({}), BUG)["resolved"])

    def test_unapplied_patch_is_not_resolved(self):
        t = _task(["t::c"])
        buggy = _run({"t::c": "FAILED"}, apply="")
        v = sandbox.verdict(t, buggy, _run({"t::c": "PASSED"}, apply="FAIL"), BUG)
        self.assertEqual((v["applied"], v["resolved"]), (False, False))

    def test_editing_the_tests_is_not_resolved(self):
        t = _task(["t::c"])
        buggy = _run({"t::c": "FAILED"}, apply="")
        cheat = BUG.replace("pkg/mod.py", "tests/test_mod.py")
        v = sandbox.verdict(t, buggy, _run({"t::c": "PASSED"}), cheat)
        self.assertEqual((v["tampered"], v["resolved"]), (True, False))

    def test_empty_f2p_never_resolves(self):
        t = _task([])
        self.assertFalse(sandbox.verdict(t, _run({}, apply=""), _run({"t::a": "PASSED"}), BUG)["resolved"])


class ExamFiles(unittest.TestCase):
    def test_every_swesmith_row_becomes_a_task_with_tests(self):
        rows = [json.loads(l) for l in (ROOT / "exams" / "exam_large.jsonl")
                .read_text(encoding="utf-8").splitlines() if l.strip()]
        sw = [to_task(r, fetch_meta=False) for r in rows if is_swesmith(r)]
        self.assertEqual(len(sw), 63)
        self.assertTrue(all(t.fail_to_pass and "::" in t.fail_to_pass[0] for t in sw))
        self.assertTrue(all(t.fix_patch != t.bug_patch for t in sw))


if __name__ == "__main__":
    unittest.main()
