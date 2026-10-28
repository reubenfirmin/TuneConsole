# Ticket writing

- Write clear, specific ticket descriptions and comments. Use technical detail
  when it helps explain the problem, proposed change, or evidence. Do not attempt
  to sound impressive or write more just to look good. Clarity is key.

# Motion

- Do not respect reduced-motion preferences. Do not disable, simplify, or pause
  animations in response to `prefers-reduced-motion`.

# Command approvals

- Do not prefix `uv` commands with environment variable assignments; they defeat
  command whitelisting. Run `uv` normally.
- Before running pytest, check the whitelisted command form and use that
  invocation.

# Test coordination

- One agent owns test execution at a time. During delegated work, the coordinator
  owns testing unless it explicitly hands that role to one worker. Other workers
  report the exact test commands their changes need and continue with edits or
  review; they do not launch their own test runs.
- Review agent will check implementation work, and is the only agent that should
  be running the full test suite.
- Review agent should check for tickets 'In Review', verify correctness,
  implementation specifics, code quality, test coverage, UI; complete tickets
  should be moved to Done, tickets needing more work should be reopened to Ready.
- Run focused checks as needed. Share the command, result, and any failures when
  handing testing back. Do not repeat an unchanged passing run just to produce
  another report.
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

# Review agent workflow

These instructions apply to the agent assigned the review role. Implementing
agents hand their work to the reviewer for independent acceptance.

## Responsibility and takeover

- The review agent independently establishes whether each ticket meets its
  agreed requirements and owns the decision to move it to Done. Author reports,
  passing tests, and the presence of implementation code do not establish
  acceptance by themselves.
- Before taking over, refresh the project board and read the relevant tickets,
  review comments, and current user instructions. Establish outstanding
  verification and test ownership from current evidence.
- Understand the agreed requirements and relevant user clarifications. If a
  handoff changes the agreed scope, stop and confirm the change with the user.

## Acceptance for each ticket

- Review correctness, design, regressions, resource usage, and relevant edge
  cases. Trace the complete relevant workflow across component boundaries.
- Run associated tests and assess whether they actually prove the requirements.
  Add focused independent checks where existing coverage assumes away likely
  failures. Follow the test coordination rules above.
- Data-dependent features require realistic data and the user's actual library
  where applicable. Synthetic fixtures alone are insufficient for accepting
  complex data behavior.
- For preparation and recommendation changes, check fresh-user startup,
  incomplete processing, missing metadata, continuing updates, and time to useful
  output. Existing prepared state must not conceal startup dependencies.
- For UI changes, inspect the rendered result and exercise relevant interactions.
  Establish which code the running application actually loaded; a changed source
  file does not prove that the running app uses it.
- Inspect the user's database through read-only access or an isolated snapshot.
  Never run tests or migrations against it to obtain review evidence.
- Record the tested requirements, commands, results, dataset, runtime version,
  and remaining gaps in the review comment. Clearly distinguish independent
  observations from author reports and identify any use of controlled fixtures.

## Disposition

- Move demonstrated failures to Ready with reproduction details, expected and
  actual behavior, and acceptance criteria. Set an appropriate priority.
- Keep required but incomplete verification In review. Missing evidence is not a
  passing result. Move to Done only when all required acceptance checks are
  established.
- Keep GitHub-dependent and release-dependent checks In review until the relevant
  push, CI run, or actual release provides evidence. The review agent owns the
  follow-up; an external dependency alone is not a reason to reopen to Ready.

## Continuous operation

- Monitor In review. When no actionable tickets remain, wait ten minutes and
  check again. Do not end monitoring merely because the queue is temporarily
  empty. Keep waits interruptible and remain responsive to user instructions.
- Continue code review and other independent work while another agent owns the
  test lock or tests are running. Do not launch competing validation or bypass
  the lock.
- Run the full suite after the current integration list is complete, subject to
  the user's current instructions. An empty review queue alone does not establish
  that deferred integration work is complete.
- File newly discovered test failures in Ready with exact test names, commands,
  results, diagnostics, and an appropriate priority. Check for existing failure
  tickets before creating duplicates.
- Keep review evidence and outstanding acceptance checks in ticket comments so
  the next reviewer can establish what remains. Keep changing ticket state out
  of these permanent instructions.
