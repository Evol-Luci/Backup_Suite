# Final review fix: missing task check profile

Base: `93d85aee204f8eb6944a6f65e90a8aff79c662c5`.

Split the missing task selection from profile mismatch in `CheckRuns.binding`.
Missing selection now raises `CHECK_PROFILE_MISSING` with a setup hint naming
`solace task check-profile select` and explaining resume/new-result/new-plan steps.
Profile mismatch retains its existing `CHECK_RUN_INVALID` branch and message.
The regression uses real ready-task/plan fixtures without a selected profile and
also verifies that the rejected request reserves no check run.

## TDD evidence

Command (run before production edit):
```sh
env SOLACE_TEST_TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests/solace_core:. python -m unittest test_check_runs.RunTests.test_missing_task_profile_returns_setup_error_without_reserving_run -v
```
Output (exit 1):
```text
AssertionError: 'CHECK_RUN_INVALID' != 'CHECK_PROFILE_MISSING'
- CHECK_RUN_INVALID
+ CHECK_PROFILE_MISSING

Ran 1 test in 0.346s

FAILED (failures=1)
```

Same command after production edit (exit 0):
```text
test_missing_task_profile_returns_setup_error_without_reserving_run (test_check_runs.RunTests.test_missing_task_profile_returns_setup_error_without_reserving_run) ... ok

----------------------------------------------------------------------
Ran 1 test in 0.349s

OK
```

Covering module command:
```sh
env SOLACE_TEST_TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests/solace_core:. python -m unittest test_check_runs -q
```
Output (exit 0):
```text
----------------------------------------------------------------------
Ran 25 tests in 14.226s

OK
```

`git diff --check` exited 0 with no output. The coordinating agent owns the
fresh full-core suite after this commit; this report makes no post-fix full-suite
claim. No installation or merge performed.
