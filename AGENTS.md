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
- Keep tests serial. Do not use pytest workers or run unit, browser, benchmark,
  or other resource-heavy validation concurrently. Coordinate checks outside
  pytest with the same test owner.
- The OS releases the lock when pytest exits, including after a crash. The lock
  file remains in `/tmp/tuneconsole-tests-<uid>.lock`; its contents describe the
  most recent owner and are only current while the OS lock is held.
