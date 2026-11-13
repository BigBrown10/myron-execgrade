# myron-execgrade

A test-execution grader for bug-fix patches.

A patch counts as **resolved** only if all of these hold:

1. it applies to the buggy code,
2. every test that was failing now passes,
3. no test that passed before now fails (within the test files that were run),
4. it does not edit test files.

Each task runs in its own Docker container with no network access, using the
prebuilt image for that task from the [SWE-smith](https://github.com/SWE-bench/SWE-smith) dataset.

## Run it

Needs Python 3.11+ and Docker. No Python packages to install.

```bash
# What is in an exam file, without running anything
python -m execgrade audit --exam exams/exam_large.jsonl

# Check tasks are usable: buggy code must fail its tests, the reference fix must pass
python -m execgrade validate --exam exams/exam_bugs_context.jsonl --limit 3

# Grade predictions. 'gold' = reference fixes (should resolve every valid task),
# 'null' = empty patches (should resolve none)
python -m execgrade grade --exam exams/exam_bugs_context.jsonl --preds gold --limit 3
python -m execgrade grade --exam exams/exam_bugs_context.jsonl --preds my_predictions.jsonl
```

A predictions file is JSON Lines, one object per task: `{"id": "...", "patch": "<unified diff>"}`.

Unit tests: `python -m unittest discover -s tests`

## In CI

The `exec-grade` workflow runs the arguments in `ci/exec_grade_args.txt` on every
push to `main` that touches the grader, and can be started by hand from the Actions tab.

## What the exam files are

`exams/*.jsonl` hold bug reports with a reference patch and the names of the failing tests.
Two kinds of row are mixed, and they point in opposite directions:

- rows whose `repo` starts with `swesmith/`: the `patch` field is the diff that
  **introduces** the bug, so the reference fix is its reverse. Only these rows can be
  run today, because each has a prebuilt image.
- other rows: `patch` is the real fix, but the row has no base commit, so the buggy
  checkout is unknown and the row is reported as not runnable.
