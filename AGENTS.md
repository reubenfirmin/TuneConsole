# Test coordination

- One agent owns test execution at a time. During delegated work, the coordinator
  owns testing unless it explicitly hands that role to one worker. Other workers
  report the exact test commands their changes need and continue with edits or
  review; they do not launch their own test runs.
- Run focused checks as needed, then run the broad suite once after integrating
  the changes. Share the command, result, and any failures when handing testing
  back. Do not repeat an unchanged passing run just to produce another report.
- Every normal pytest invocation acquires the same per-user machine lock, across
  TuneConsole checkouts and worktrees. A busy lock reports the owner's PID,
  checkout, start time, and command before collecting tests. Respect that owner;
  do not bypass the lock, delete its file, or kill another agent's test process.
- Use `uv run pytest <paths>` for focused checks. If a run must wait for the
  current owner, use `uv run pytest --test-lock-wait=60 <paths>` with a bounded
  wait. A busy run exits immediately by default, so workers can hand their test
  requests to the owner instead of accumulating waiting processes.
- Full local pytest runs use up to four local worker processes by default, with
  whole files kept together. Use `-n 0` for a serial full run or `-n N` to choose
  the count. Focused checks, CI shards, collection, debugging and live/package
  acceptance checks stay serial.
- One coordinator still owns the machine lock for the entire run. Its workers
  share a separate lifetime lock so a killed coordinator cannot admit a new
  session while its workers are finishing. Do not launch separate unit, browser,
  benchmark or other resource-heavy sessions concurrently. Coordinate checks
  outside pytest with the same test owner.
- The OS releases locks when their owning processes exit, including after a
  crash. Lock files remain at `/tmp/tuneconsole-tests-<uid>.lock` and
  `/tmp/tuneconsole-tests-<uid>-workers.lock`; never delete either file. Owner
  details describe the most recent coordinator and are only current while either
  lock is held.
