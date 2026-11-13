"""Unified-diff helpers: reverse a patch, list its files, pull a patch out of raw model text."""
import re

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_TEST_PATH = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$")


def files_in_patch(patch):
    """Paths a patch touches (both sides, /dev/null excluded)."""
    out = []
    for m in re.finditer(r"^(?:---|\+\+\+) (?:[ab]/)?(\S+)", patch, flags=re.M):
        p = m.group(1)
        if p != "/dev/null" and p not in out:
            out.append(p)
    return out


def touches_tests(patch):
    """True if the patch edits a test file: a candidate that does is rejected."""
    return any(_TEST_PATH.search(p) for p in files_in_patch(patch))


def reverse_patch(patch):
    """Return the patch that undoes `patch` (same result as `git apply -R`).

    SWE-smith stores the diff that INTRODUCES the bug, so the reference fix
    is its reverse.
    """
    out = []
    in_hunk = False
    lines = patch.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _HUNK.match(line)
        if m:
            a, b, c, d, tail = m.groups()
            old = f"{c}" + (f",{d}" if d is not None else "")
            new = f"{a}" + (f",{b}" if b is not None else "")
            out.append(f"@@ -{old} +{new} @@{tail}")
            in_hunk = True
        elif line.startswith("diff --git "):
            in_hunk = False
            out.append(line)
        elif not in_hunk and line.startswith("--- ") and i + 1 < len(lines) \
                and lines[i + 1].startswith("+++ "):
            old_path, new_path = line[4:], lines[i + 1][4:]
            out.append("--- " + _swap_prefix(new_path, "a/"))
            out.append("+++ " + _swap_prefix(old_path, "b/"))
            i += 1
        elif not in_hunk and line.startswith("new file mode "):
            out.append("deleted file mode " + line[len("new file mode "):])
        elif not in_hunk and line.startswith("deleted file mode "):
            out.append("new file mode " + line[len("deleted file mode "):])
        elif not in_hunk and line.startswith("index "):
            m2 = re.match(r"index ([0-9a-f]+)\.\.([0-9a-f]+)(.*)$", line)
            out.append(f"index {m2.group(2)}..{m2.group(1)}{m2.group(3)}" if m2 else line)
        elif in_hunk and line.startswith("+"):
            out.append("-" + line[1:])
        elif in_hunk and line.startswith("-"):
            out.append("+" + line[1:])
        else:
            out.append(line)
        i += 1
    return _reorder_hunk_bodies("\n".join(out))


def _swap_prefix(path, want):
    if path == "/dev/null":
        return path
    if path[:2] in ("a/", "b/"):
        return want + path[2:]
    return path


def _reorder_hunk_bodies(patch):
    """Inside each change block put '-' lines before '+' lines (canonical order)."""
    out, minus, plus = [], [], []

    def flush():
        out.extend(minus)
        out.extend(plus)
        minus.clear()
        plus.clear()

    in_hunk = False
    last = out
    for line in patch.split("\n"):
        if line.startswith("@@ "):
            flush()
            in_hunk = True
            out.append(line)
            last = out
        elif line.startswith("diff --git "):
            flush()
            in_hunk = False
            out.append(line)
            last = out
        elif in_hunk and line.startswith("\\"):
            # "\ No newline at end of file" belongs to the line just before it
            last[-1] = last[-1] + "\n" + line
        elif in_hunk and line.startswith("-"):
            minus.append(line)
            last = minus
        elif in_hunk and line.startswith("+"):
            plus.append(line)
            last = plus
        else:
            flush()
            out.append(line)
            last = out
    flush()
    return "\n".join(out)


def extract_patch(raw):
    """Pull a unified diff out of raw model output. Returns '' if there is none.

    Accepts a fenced ```diff block, or a bare diff starting at 'diff --git' or
    '--- a/'. Trailing prose after the last hunk line is dropped.
    """
    if not raw:
        return ""
    m = re.search(r"```(?:diff|patch)?\n(.*?)```", raw, flags=re.S)
    text = m.group(1) if m and ("@@" in m.group(1)) else raw
    start = None
    for pat in (r"^diff --git ", r"^--- (?:a/)?\S+\n\+\+\+ "):
        s = re.search(pat, text, flags=re.M)
        if s and (start is None or s.start() < start):
            start = s.start()
    if start is None or "@@" not in text[start:]:
        return ""
    kept = []
    in_hunk = False
    for line in text[start:].split("\n"):
        if line.startswith(("diff --git ", "index ", "--- ", "+++ ", "new file mode",
                            "deleted file mode", "similarity index", "rename ")):
            in_hunk = False
            kept.append(line)
        elif line.startswith("@@"):
            in_hunk = True
            kept.append(line)
        elif in_hunk and (line[:1] in (" ", "+", "-", "\\") or line == ""):
            kept.append(line if line else " ")
        else:
            break
    while kept and kept[-1].strip() == "":
        kept.pop()
    return "\n".join(kept) + "\n" if kept else ""
