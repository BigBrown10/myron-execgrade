"""Turn exam rows into honest tasks: which state is buggy, what the fix is, which tests decide.

Two row families live in the exams, and they point in OPPOSITE directions:

  swesmith/*  rows: `patch` INTRODUCES the bug (clean -> buggy). The mirror
                    repo's default branch is the clean code; the bug lives on
                    the branch named after the instance id. Fix = reverse(patch).
  other rows:       `patch` is the real fix (buggy -> fixed), but the row has
                    no base commit, so the buggy checkout must be looked up.
"""
import ast
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .patch import files_in_patch, reverse_patch

CACHE = Path(__file__).parent / "cache"
HF_FILTER = "https://datasets-server.huggingface.co/filter"
SWESMITH_DATASET = "SWE-bench/SWE-smith"


def parse_tests(x):
    """fail_tests is stored as the *repr of a list* in the exams; return a real list."""
    if isinstance(x, list):
        return [str(t) for t in x]
    if not x:
        return []
    try:
        v = ast.literal_eval(x)
        return [str(t) for t in v] if isinstance(v, (list, tuple)) else [str(v)]
    except (ValueError, SyntaxError):
        return re.findall(r"'([^']+)'", x)


def is_swesmith(row):
    return (row.get("repo") or "").startswith("swesmith/")


@dataclass
class Task:
    id: str
    repo: str
    family: str                 # "swesmith" | "real"
    issue: str
    bug_patch: str              # clean -> buggy (swesmith only, else "")
    fix_patch: str              # buggy -> fixed (the reference solution)
    fail_to_pass: list
    pass_to_pass: list = field(default_factory=list)
    image: str = ""
    alt_image: str = ""         # the image name the dataset itself lists, tried second
    tier: str = ""

    @property
    def gold_files(self):
        return [p for p in files_in_patch(self.fix_patch)]

    @property
    def runnable(self):
        return self.family == "swesmith" and bool(self.image) and bool(self.fail_to_pass)


def swesmith_image(repo):
    """swesmith/Owner__name.hash -> swebench/swesmith.x86_64.owner_1776_name.hash"""
    name = repo.split("/", 1)[1].replace("__", "_1776_").lower()
    return f"swebench/swesmith.x86_64.{name}"


def _hf_row(instance_id, timeout=60):
    q = urllib.parse.urlencode({
        "dataset": SWESMITH_DATASET, "config": "default", "split": "train",
        "where": f"\"instance_id\"='{instance_id}'", "length": 1})
    req = urllib.request.Request(f"{HF_FILTER}?{q}", headers={"User-Agent": "execgrade"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    rows = d.get("rows") or []
    return rows[0]["row"] if rows else None


def swesmith_meta(instance_id, offline=False):
    """FAIL_TO_PASS / PASS_TO_PASS / image for one instance, cached on disk."""
    CACHE.mkdir(exist_ok=True)
    f = CACHE / "swesmith_meta.jsonl"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r["instance_id"] == instance_id:
                    return r
    if offline:
        return None
    try:
        row = _hf_row(instance_id)
    except (OSError, ValueError):       # network down or a bad reply: fall back to the exam row
        return None
    if not row:
        return None
    rec = {"instance_id": instance_id,
           "FAIL_TO_PASS": parse_tests(row.get("FAIL_TO_PASS")),
           "PASS_TO_PASS": parse_tests(row.get("PASS_TO_PASS")),
           "image_name": row.get("image_name") or ""}
    with f.open("a", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(rec) + "\n")
    return rec


def to_task(row, offline=False, fetch_meta=True):
    f2p = parse_tests(row.get("fail_tests"))
    if is_swesmith(row):
        meta = swesmith_meta(row["id"], offline=offline) if fetch_meta else None
        image = swesmith_image(row["repo"])
        p2p, alt = [], ""
        if meta:
            f2p = meta["FAIL_TO_PASS"] or f2p
            p2p = meta["PASS_TO_PASS"]
            alt = meta.get("image_name") or ""
        return Task(id=row["id"], repo=row["repo"], family="swesmith",
                    issue=row.get("problem_statement") or "",
                    bug_patch=row["patch"], fix_patch=reverse_patch(row["patch"]),
                    fail_to_pass=f2p, pass_to_pass=p2p, image=image,
                    alt_image=alt if alt != image else "", tier=row.get("tier") or "")
    return Task(id=row["id"], repo=row.get("repo") or "", family="real",
                issue=row.get("problem_statement") or "", bug_patch="",
                fix_patch=row["patch"], fail_to_pass=f2p, tier=row.get("tier") or "")


def load_exam(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines()
            if l.strip()]
