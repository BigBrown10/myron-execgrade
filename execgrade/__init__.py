"""execgrade - test-execution grader for Myron.

The only score that counts: apply the candidate patch to the BUGGY checkout,
run the bug's failing tests in a sandbox, and require them to pass without
breaking tests that passed before.

    python -m execgrade audit    --exam exams/exam_large.jsonl
    python -m execgrade validate --exam exams/exam_large.jsonl --limit 5
    python -m execgrade grade    --exam exams/exam_large.jsonl --preds preds.jsonl
"""
