"""Run a task's tests inside its SWE-smith Docker image. No network, capped CPU/RAM.

One container per run. Model-written code never executes on the host.
"""
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .patch import touches_tests

RUN_SH = r"""#!/bin/bash
cd /testbed || { echo "@@EXEC no_testbed"; exit 0; }
source /opt/miniconda3/bin/activate 2>/dev/null
conda activate testbed 2>/dev/null
git config --global --add safe.directory /testbed 2>/dev/null
cp /eval/*.patch /tmp/ 2>/dev/null
echo "@@DIAG head=$(git rev-parse --short HEAD 2>/dev/null) dirty=$(git status --porcelain 2>/dev/null | wc -l) python=$(command -v python) $(python --version 2>&1)"

if [ -s /tmp/bug.patch ]; then
  if git apply --whitespace=nowarn /tmp/bug.patch 2>/tmp/err; then echo "@@EXEC bug_apply=OK"
  else echo "@@EXEC bug_apply=FAIL"; head -5 /tmp/err; exit 0; fi
fi

if [ -f /tmp/cand.patch ]; then
  if git apply --whitespace=nowarn /tmp/cand.patch 2>/tmp/err; then echo "@@EXEC cand_apply=git"
  elif patch -p1 -F3 --no-backup-if-mismatch -s < /tmp/cand.patch >/tmp/err 2>&1; then echo "@@EXEC cand_apply=fuzz"
  else echo "@@EXEC cand_apply=FAIL"; head -5 /tmp/err; exit 0; fi
fi

echo "@@EXEC tests_begin"
timeout __TIMEOUT__ python -m pytest -rA --tb=no --color=no -q -p no:cacheprovider \
  --disable-warnings __FILES__ 2>&1
echo "@@EXEC pytest_rc=$?"
"""

_STATUS = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+(\S.*)$")


@dataclass
class Run:
    ok: bool = False                 # container ran to the end of the script
    bug_apply: str = ""              # OK | FAIL | "" (not requested)
    cand_apply: str = ""             # git | fuzz | FAIL | "" (no candidate)
    pytest_rc: int = -1
    statuses: dict = field(default_factory=dict)   # test id -> status
    log_tail: str = ""
    error: str = ""
    diag: str = ""                   # what the container looked like: commit, python

    def passed(self):
        return {t for t, s in self.statuses.items() if s == "PASSED"}


def docker_ready():
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def have_image(image):
    return subprocess.run(["docker", "image", "inspect", image],
                          capture_output=True).returncode == 0


def pull(image, timeout=3600):
    r = subprocess.run(["docker", "pull", "-q", image], capture_output=True,
                       text=True, timeout=timeout)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[-300:]


def remove_image(image):
    """Free the disk an image takes (CI runners have little)."""
    subprocess.run(["docker", "rmi", "-f", image], capture_output=True)


def test_files(test_ids):
    out = []
    for t in test_ids:
        f = t.split("::", 1)[0]
        if f and f not in out:
            out.append(f)
    return out


def parse_statuses(log):
    """pytest -rA summary lines -> {test id: status}. FAILED lines carry ' - reason'."""
    st = {}
    for line in log.splitlines():
        m = _STATUS.match(line.strip())
        if not m:
            continue
        status, rest = m.group(1), m.group(2)
        if status in ("FAILED", "ERROR") and " - " in rest:
            rest = rest.split(" - ", 1)[0]
        st[rest.strip()] = status
    return st


def run(task, cand_patch=None, apply_bug=True, timeout=900, mem="4g", cpus="2"):
    """Run task's fail-to-pass test files. cand_patch=None means 'buggy state as is'."""
    files = test_files(task.fail_to_pass)
    if not files:
        return Run(error="no test files")
    work = Path(tempfile.mkdtemp(prefix="execgrade_"))
    try:
        (work / "bug.patch").write_text(task.bug_patch if apply_bug else "",
                                        encoding="utf-8", newline="\n")
        if cand_patch is not None:
            (work / "cand.patch").write_text(
                cand_patch if cand_patch.endswith("\n") else cand_patch + "\n",
                encoding="utf-8", newline="\n")
        script = (RUN_SH.replace("__TIMEOUT__", str(timeout))
                  .replace("__FILES__", " ".join(_shq(f) for f in files)))
        (work / "run.sh").write_text(script, encoding="utf-8", newline="\n")
        cmd = ["docker", "run", "--rm", "--network", "none", "--memory", mem,
               "--cpus", cpus, "--pids-limit", "1024",
               "-v", f"{work}:/eval:ro", task.image, "bash", "/eval/run.sh"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout + 120)
        except subprocess.TimeoutExpired:
            return Run(error="container timeout")
        log = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
        r = Run(log_tail=log[-1500:])
        m = re.search(r"@@DIAG (.*)", log)
        r.diag = m.group(1).strip() if m else ""
        m = re.search(r"@@EXEC bug_apply=(\w+)", log)
        r.bug_apply = m.group(1) if m else ""
        m = re.search(r"@@EXEC cand_apply=(\w+)", log)
        r.cand_apply = m.group(1) if m else ""
        m = re.search(r"@@EXEC pytest_rc=(-?\d+)", log)
        if m:
            r.pytest_rc = int(m.group(1))
            r.ok = True
        elif "@@EXEC" in log:
            r.ok = True          # stopped early on purpose (a patch did not apply)
        else:
            r.error = "container did not run the script: " + log[-200:]
        if "@@EXEC tests_begin" in log:
            r.statuses = parse_statuses(log.split("@@EXEC tests_begin", 1)[1])
        return r
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def verdict(task, buggy, cand, cand_patch):
    """Decide one candidate. `buggy` is the same test files run on the buggy state."""
    f2p = list(task.fail_to_pass)
    v = {"applied": cand.cand_apply in ("git", "fuzz"), "apply_method": cand.cand_apply,
         "tampered": touches_tests(cand_patch or ""), "f2p_total": len(f2p),
         "f2p_passed": 0, "regressions": 0, "resolved": False}
    if not v["applied"]:
        return v
    now = cand.passed()
    v["f2p_passed"] = sum(1 for t in f2p if t in now)
    # anything green before the patch, in the files we ran, must stay green
    v["regressions"] = len(buggy.passed() - now)
    v["resolved"] = (v["f2p_passed"] == len(f2p) and len(f2p) > 0
                     and v["regressions"] == 0 and not v["tampered"])
    return v


def validate(task, timeout=900):
    """A task is usable only if the buggy state is RED and the reference fix is GREEN."""
    buggy = run(task, None, timeout=timeout)
    out = {"id": task.id, "image": task.image, "f2p_total": len(task.fail_to_pass),
           "bug_apply": buggy.bug_apply, "valid": False, "why": "", "container": buggy.diag,
           "tests_seen_on_buggy": len(buggy.statuses)}
    if not buggy.ok or buggy.bug_apply != "OK":
        out["why"] = buggy.error or f"bug patch did not apply ({buggy.bug_apply})"
        out["log_tail"] = buggy.log_tail[-400:]
        return out, buggy
    red_pass = [t for t in task.fail_to_pass if buggy.statuses.get(t) == "PASSED"]
    out["f2p_passing_on_buggy"] = len(red_pass)
    out["f2p_seen_on_buggy"] = sum(1 for t in task.fail_to_pass if t in buggy.statuses)
    gold = run(task, task.fix_patch, timeout=timeout)
    v = verdict(task, buggy, gold, task.fix_patch)
    out["gold"] = v
    if red_pass:
        out["why"] = f"{len(red_pass)} fail-to-pass tests already pass on the buggy state"
    elif not v["resolved"]:
        out["why"] = "reference fix does not turn the failing tests green"
        out["log_tail"] = gold.log_tail[-400:]
    else:
        out["valid"] = True
    return out, buggy
