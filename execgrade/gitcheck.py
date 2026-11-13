"""gitcheck - prove, with git alone (nothing is executed), what each SWE-smith row really is.

For every swesmith row of an exam:
  1. fetch the instance branch (the BUGGY code) and the default branch (the CLEAN code)
  2. confirm the row's `patch` does NOT apply to the buggy branch but its reverse DOES
  3. confirm buggy + reverse(patch) reproduces the clean branch's files exactly

Run: python -m execgrade.gitcheck --exam exams/exam_large.jsonl [--keep DIR]
With --keep DIR the buggy checkouts stay on disk at DIR/<task id>/ for later use.
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .patch import files_in_patch
from .tasks import is_swesmith, load_exam, to_task

ROOT = Path(__file__).resolve().parent.parent


def git(cwd, *args, stdin=None, timeout=600):
    r = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "advice.detachedHead=false",
                        *args], cwd=cwd, input=stdin.encode("utf-8") if stdin else None,
                       capture_output=True, timeout=timeout)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def check(task, dest):
    """-> dict of facts about one task. `dest` ends up holding the buggy checkout."""
    out = {"id": task.id, "ok": False}
    dest.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{task.repo}.git"
    have = (dest / ".git").exists() and all(
        git(dest, "rev-parse", "-q", "--verify", b)[0] == 0 for b in ("buggy", "clean"))
    if have:                                    # reuse a kept checkout: no network
        git(dest, "checkout", "-q", "-f", "buggy")
        git(dest, "clean", "-fdq")
    else:
        git(dest, "init", "-q")
        rc, _, err = git(dest, "fetch", "-q", "--depth", "1", url, task.id)
        if rc:
            out["why"] = "no instance branch: " + err.strip()[-120:]
            return out
        git(dest, "checkout", "-q", "-B", "buggy", "FETCH_HEAD")
        rc, _, err = git(dest, "fetch", "-q", "--depth", "1", url, "HEAD")
        if rc:
            out["why"] = "no default branch: " + err.strip()[-120:]
            return out
        git(dest, "branch", "-f", "clean", "FETCH_HEAD")

    out["bug_patch_applies_to_buggy"] = git(dest, "apply", "--check", "-", stdin=task.bug_patch)[0] == 0
    rc, _, err = git(dest, "apply", "--check", "-", stdin=task.fix_patch)
    out["fix_applies_to_buggy"] = rc == 0
    if rc:
        out["why"] = "reversed patch does not apply: " + err.strip()[-160:]
        return out
    git(dest, "apply", "-", stdin=task.fix_patch)
    files = files_in_patch(task.fix_patch)
    git(dest, "add", "-A", "--", *files)             # stage, so re-created files count
    rc, diff, _ = git(dest, "diff", "--cached", "--stat", "clean", "--", *files)
    out["fixed_equals_clean"] = (rc == 0 and diff.strip() == "")
    git(dest, "reset", "-q", "--hard", "buggy")      # back to the buggy state
    git(dest, "clean", "-fdq")
    rc, names, _ = git(dest, "diff", "--name-only", "clean", "buggy")
    out["files_differing_buggy_vs_clean"] = len([n for n in names.split() if n])
    out["ok"] = out["fix_applies_to_buggy"] and out["fixed_equals_clean"]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exam", required=True)
    ap.add_argument("--keep", default="", help="directory to keep buggy checkouts in")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tasks = [to_task(r, fetch_meta=False) for r in load_exam(a.exam) if is_swesmith(r)]
    if a.limit:
        tasks = tasks[:a.limit]
    out_path = Path(a.out or ROOT / "results" / "exec" / f"gitcheck_{Path(a.exam).stem}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    base = Path(a.keep) if a.keep else Path(tempfile.mkdtemp(prefix="gitcheck_"))
    res = []
    try:
        for k, t in enumerate(tasks):
            dest = base / t.id
            try:
                rec = check(t, dest)
            except subprocess.TimeoutExpired:
                rec = {"id": t.id, "ok": False, "why": "git timeout"}
            res.append(rec)
            print(f"[{k+1}/{len(tasks)}] {t.id[:62]}  ok={rec['ok']}  {rec.get('why','')}", flush=True)
            if not a.keep:
                shutil.rmtree(dest, ignore_errors=True)
            summary = {
                "exam": str(a.exam), "checked": len(res),
                "reference_fix_applies_and_reproduces_clean": sum(r["ok"] for r in res),
                "stored_patch_applies_to_buggy_state": sum(bool(r.get("bug_patch_applies_to_buggy")) for r in res),
                "rows": res}
            out_path.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    finally:
        if not a.keep:
            shutil.rmtree(base, ignore_errors=True)
    n = len(res)
    print(f"\nreverse(patch) applies to the buggy branch and reproduces clean: "
          f"{sum(r['ok'] for r in res)}/{n}")
    print(f"stored `patch` applies to the buggy branch (it should not): "
          f"{sum(bool(r.get('bug_patch_applies_to_buggy')) for r in res)}/{n}")
    print(f"-> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
