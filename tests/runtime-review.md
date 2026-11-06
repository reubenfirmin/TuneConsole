# Test runtime findings

Historical inventory for [#534](https://github.com/reubenfirmin/TuneConsole/issues/534),
completed November 3, 2026. Test commands and fixture rules live in
[README.md](README.md).

## Conclusions

- Repeated schema construction, large unrelated fixtures, repeated model fits
  and duplicated browser journeys were major costs. Reuse genuine empty-schema
  templates and give each consumer a private database and file namespace.
- Bootstrap, migrations, admission and corruption checks must still exercise
  their real subject operations. Historical-format tests need genuine versions.
- Share setup when cases test the same prepared input. Preserve distinct
  failure, recovery, concurrency, ownership and negative cases.
- Keep production-scale checks in explicit benchmarks. Smaller ordinary fixtures
  must retain the boundary that caused the bug.
- Covered-line overlap identifies review candidates; it does not prove that
  assertions or workflows are equivalent.

## Recorded inventory

13,299 unique cases ended: 13,170 passed, 85 failed and 44 skipped. Four
instrumented segments took 13,674.60 seconds, including repeated attempts.
This was a multi-segment diagnostic inventory, not a clean full-suite run or
a passing 15-minute performance result.

Reports: `build/test-reports/534-full-analysis/`. They contain collection
accounting, timings, failure owners, coverage, overlap groups and source hashes.
The interrupted segments and coverage-collision recovery are recorded there.
Failure dispositions and remaining integration work belong in #534 and its
linked tickets; these counts do not describe the current checkout.
