"""CLI: audit an exam, validate tasks, grade predictions by running tests.

    python -m execgrade audit    --exam exams/exam_large.jsonl
    python -m execgrade validate --exam exams/exam_large.jsonl --limit 5
    python -m execgrade grade    --exam exams/exam_large.jsonl --preds gold
    python -m execgrade grade    --exam exams/exam_large.jsonl --preds results/x.json

--preds accepts: 'gold' (reference fixes; must score 100% of valid tasks),
'null' (empty patches; must score 0), a .jsonl of {"id","patch"} or {"id","raw"},
or one of the repo's result files ({"rows":[{"id","raw"}]}).
"""
import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

from . import sandbox
from .patch import extract_patch
from .tasks import is_swesmith, load_exam, parse_tests, to_task

ROOT = Path(__file__).resolve().parent.parent


def cmd_audit(a):
    rows = load_exam(a.exam)
    n = len(rows)
    fam = Counter("swesmith" if is_swesmith(r) else "real" for r in rows)
    str_tests = sum(isinstance(r.get("fail_tests"), str) for r in rows)
    shown = Counter(", ".join((r.get("fail_tests") or [])[:3]) for r in rows).most_common(1)

    def gold(p):
        return set(re.findall(r"^\+\+\+ b/(\S+)", p, flags=re.M))

    def heads(g):
        return re.findall(r"^=== (\S+) \(excerpt\) ===", g or "", flags=re.M)

    leak = sum(bool(gold(r["patch"]) & set(heads(r.get("grounding")))) for r in rows)
    echo = 0
    for r in rows:
        h = heads(r.get("grounding"))
        ans = (h[0] if h else "").lower()
        echo += any(len(Path(g).stem) >= 4 and Path(g).stem.lower() in ans
                    for g in gold(r["patch"]))
    out = {
        "exam": str(a.exam), "rows": n, "families": dict(fam),
        "fail_tests_stored_as_string": str_tests,
        "failing_tests_line_the_model_saw": shown[0][0] if shown else "",
        "prompt_names_gold_file": leak,
        "echo_first_grounding_header_lenient_score": echo,
        "rows_where_patch_introduces_the_bug": fam.get("swesmith", 0),
        "rows_with_no_buggy_checkout_info": fam.get("real", 0),
    }
    print(f"exam: {a.exam}  ({n} rows)")
    print(f"  families ............................ {dict(fam)}")
    print(f"  fail_tests stored as a string ....... {str_tests}/{n}"
          f"   -> prompt line was: FAILING TESTS: {out['failing_tests_line_the_model_saw']!r}")
    print(f"  prompt already names a gold file .... {leak}/{n}")
    print(f"  'echo the prompt' baseline (lenient)  {echo}/{n} = {echo/n:.1%}   (0 parameters)")
    print(f"  `patch` INTRODUCES the bug .......... {fam.get('swesmith', 0)}/{n}  (swesmith rows; fix = reverse)")
    print(f"  no base commit (buggy state unknown)  {fam.get('real', 0)}/{n}  (real-repo rows)")
    if a.out:
        Path(a.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


def _select(rows, a):
    if a.ids:
        want = set(a.ids.split(","))
        rows = [r for r in rows if r["id"] in want]
    tasks = [to_task(r) for r in rows if is_swesmith(r)]
    skipped = sum(1 for r in rows if not is_swesmith(r))
    if a.limit:
        tasks = tasks[:a.limit]
    return tasks, skipped


def _ensure_image(t, a):
    """Make one of the task's image names available; t.image ends up as the one that worked."""
    names = [n for n in (t.image, t.alt_image) if n]
    for name in names:
        if sandbox.have_image(name):
            t.image = name
            return True
    if a.no_pull:
        return False
    for name in names:
        print(f"    pulling {name} ...", flush=True)
        ok, msg = sandbox.pull(name)
        if ok:
            t.image = name
            return True
        print(f"    pull failed: {msg}", flush=True)
    return False


def cmd_validate(a):
    if not sandbox.docker_ready():
        print("docker is not running", file=sys.stderr)
        return 2
    tasks, skipped = _select(load_exam(a.exam), a)
    out_path = Path(a.out or ROOT / "results" / "exec" / f"validity_{Path(a.exam).stem}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = {}
    if out_path.exists():
        done = {r["id"]: r for r in json.loads(out_path.read_text(encoding="utf-8"))["tasks"]}
    res = list(done.values())
    for k, t in enumerate(tasks):
        if t.id in done and not a.redo:
            continue
        t0 = time.perf_counter()
        if not _ensure_image(t, a):
            rec = {"id": t.id, "image": t.image, "valid": False, "why": "image unavailable"}
        else:
            rec, _ = sandbox.validate(t, timeout=a.timeout)
        rec["seconds"] = round(time.perf_counter() - t0, 1)
        if a.rm_image:
            sandbox.remove_image(t.image)
        res = [r for r in res if r["id"] != t.id] + [rec]
        print(f"[{k+1}/{len(tasks)}] {t.id[:60]}  valid={rec['valid']}  "
              f"{rec.get('why','')}  ({rec['seconds']}s)", flush=True)
        _write_validity(out_path, a.exam, res, skipped)
    _write_validity(out_path, a.exam, res, skipped)
    nv = sum(r["valid"] for r in res)
    print(f"\nvalid tasks: {nv}/{len(res)} checked "
          f"({skipped} real-repo rows not runnable: no buggy checkout)  -> {out_path}")
    return 0


def _write_validity(path, exam, res, skipped):
    path.write_text(json.dumps({"exam": str(exam), "checked": len(res),
                                "valid": sum(r["valid"] for r in res),
                                "not_runnable_real_rows": skipped, "tasks": res},
                               indent=1), encoding="utf-8")


def load_preds(spec, tasks):
    """-> {task id: patch text ('' when the answer holds no patch)}"""
    if spec == "gold":
        return {t.id: t.fix_patch for t in tasks}
    if spec == "null":
        return {t.id: "" for t in tasks}
    p = Path(spec)
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".jsonl":
        rows = [json.loads(l) for l in text.splitlines() if l.strip()]
    else:
        d = json.loads(text)
        rows = d["rows"] if isinstance(d, dict) else d
    preds = {}
    for r in rows:
        patch = r.get("patch") if r.get("patch") is not None else extract_patch(r.get("raw", ""))
        preds[r["id"]] = patch or ""
    return preds


def cmd_grade(a):
    if not sandbox.docker_ready():
        print("docker is not running", file=sys.stderr)
        return 2
    tasks, skipped = _select(load_exam(a.exam), a)
    preds = load_preds(a.preds, tasks)
    name = a.preds if a.preds in ("gold", "null") else Path(a.preds).stem
    out_path = Path(a.out or ROOT / "results" / "exec" / f"grade_{Path(a.exam).stem}__{name}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    res = []
    for k, t in enumerate(tasks):
        patch = preds.get(t.id, "")
        rec = {"id": t.id, "tier": t.tier, "has_patch": bool(patch.strip()),
               "applied": False, "resolved": False, "valid_task": None}
        if not _ensure_image(t, a):
            rec["note"] = "image unavailable"
        else:
            buggy = sandbox.run(t, None, timeout=a.timeout)
            red = not any(buggy.statuses.get(x) == "PASSED" for x in t.fail_to_pass)
            rec["valid_task"] = bool(buggy.ok and buggy.bug_apply == "OK" and red)
            if rec["valid_task"] and rec["has_patch"]:
                cand = sandbox.run(t, patch, timeout=a.timeout)
                rec.update(sandbox.verdict(t, buggy, cand, patch))
            if a.rm_image:
                sandbox.remove_image(t.image)
        res.append(rec)
        print(f"[{k+1}/{len(tasks)}] {t.id[:56]}  patch={rec['has_patch']} "
              f"applied={rec['applied']} resolved={rec['resolved']}", flush=True)
        _write_grade(out_path, a, res, skipped)
    s = _write_grade(out_path, a, res, skipped)
    print(f"\n==== {name} on {Path(a.exam).name} ====")
    print(f"runnable tasks graded : {s['graded']}  (valid: {s['valid_tasks']})")
    print(f"answers with a patch  : {s['with_patch']}")
    print(f"patch applied         : {s['applied']}")
    print(f"RESOLVED (tests pass) : {s['resolved']}/{s['valid_tasks']}")
    print(f"not runnable          : {skipped} real-repo rows (reported, never counted as solved)")
    print(f"-> {out_path}")
    return 0


def _write_grade(path, a, res, skipped):
    s = {"exam": str(a.exam), "preds": str(a.preds), "graded": len(res),
         "valid_tasks": sum(bool(r["valid_task"]) for r in res),
         "with_patch": sum(r["has_patch"] for r in res),
         "applied": sum(r["applied"] for r in res),
         "resolved": sum(r["resolved"] for r in res),
         "not_runnable_real_rows": skipped}
    path.write_text(json.dumps({**s, "rows": res}, indent=1), encoding="utf-8")
    return s


def main(argv=None):
    ap = argparse.ArgumentParser(prog="execgrade", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("audit", "validate", "grade"):
        p = sub.add_parser(name)
        p.add_argument("--exam", required=True)
        p.add_argument("--out", default="")
        if name != "audit":
            p.add_argument("--limit", type=int, default=0)
            p.add_argument("--ids", default="", help="comma-separated task ids")
            p.add_argument("--timeout", type=int, default=900)
            p.add_argument("--no-pull", action="store_true",
                           help="only use images already on this machine")
            p.add_argument("--rm-image", action="store_true",
                           help="delete each image after its task (for small disks)")
        if name == "validate":
            p.add_argument("--redo", action="store_true")
        if name == "grade":
            p.add_argument("--preds", required=True)
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return {"audit": cmd_audit, "validate": cmd_validate, "grade": cmd_grade}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
