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

# Schema changes

- Keep regular schema migrations bounded. Bulk data conversions belong in
  resumable one-time preparation scripts with separate progress and recovery;
  they must not hold up regular migrations or ordinary application use.
- Keep supported installed formats compatible with reads and continuing writes
  while preparation is incomplete. Retiring old storage and compatibility code
  belongs to a separately tracked future cleanup after verification and cutover.
- Preserve applied migration history. Do not mark unfinished conversion complete
  to let the migration runner advance.

# Test coordination

- CI tests run only on pull requests. Main pushes, manual main runs and releases
  must never run unit/browser suites, packaged smoke tests or synthetic publisher
  rehearsals. Releases build and publish from main without requiring PR or CI test
  history. Do not add a test-history gate to releases.
- Keep `.pytest_cache` for pytest's own cache data. Do not leave database copies,
  source snapshots, dated run directories, logs, or screenshots there.
- Use `tmp_path` / `tmp_path_factory` for test fixtures. Pytest removes temporary
  data after passing and failing runs, including an explicit `--basetemp`.
  Save required reports outside the temporary directory before teardown.
- For experiments spanning multiple commands, use a `TemporaryDirectory` context
  and clean it on success and failure. A disk-backed workspace may live under
  `.pytest_cache` only for that context's lifetime. Do not leave retained fixtures
  there for a future run; regenerate them from a read-only source when needed.
- After an interrupted or killed run, remove its disposable workspace once its
  processes have stopped. Keep acceptance evidence in ticket comments or an
  explicitly chosen report destination, separate from disposable fixtures.
- Targeted pytest checks do not acquire or wait for the global-suite lock. Run
  `uv run pytest <paths>` directly, including while another agent owns that lock.
- The shared test lock is only for the global test suite. Running experiments
  does not require it: dataset audits, model fitting, research evaluation,
  artifact verification and focused research checks must not acquire or wait
  for the global-suite lock. A running test suite is not a blocker for experiments.
- One agent owns global-suite execution at a time. During delegated work, the
  coordinator owns that execution unless it explicitly hands the role to one
  worker. Other workers may run their focused checks and experiments.
- Review agent will check implementation work, and is the only agent that should
  be running the full test suite.
- Review agent should check for tickets 'In Review', verify correctness,
  implementation specifics, code quality, test coverage, UI; complete tickets
  should be moved to Done, tickets needing more work should be reopened to Ready.
- Run focused checks as needed. Share the command, result, and any failures when
  handing testing back. Do not repeat an unchanged passing run just to produce
  another report.
- Global-suite runs share the same per-user machine lock across TuneConsole
  checkouts and worktrees. Respect that owner; do not bypass the lock for another
  global-suite run, delete its file, or kill another agent's test process.
- Use `uv run pytest <paths>` for focused checks and `./experiments/run.py check`
  for research checks. Only a global-suite run needs the bounded
  `--test-lock-wait=60` wait. If an older experiment entrypoint acquires the
  global-suite lock, remove that dependency rather than waiting for the suite.
- Full local pytest runs use up to four local worker processes by default, with
  whole files kept together. Use `-n 0` for a serial full run or `-n N` to choose
  the count. Focused checks, CI shards, collection, debugging and live/package
  acceptance checks stay serial.
- One coordinator owns the machine lock for the entire global-suite run. Its workers
  share a separate lifetime lock so a killed coordinator cannot admit a new
  global-suite session while its workers are finishing. Keep experiments within
  their own resource bounds; they do not share this lock or require permission
  from the global-suite owner.
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
  related feature tickets, design decisions, screenshots, prior sprint reviews,
  review comments, and current user instructions. Establish outstanding
  verification and test ownership from current evidence.
- Reconstruct the current acceptance criteria across that feature history.
  Later user corrections require reassessing affected behavior and earlier
  acceptance claims, including behavior in tickets already marked Done.
- Understand the agreed requirements and relevant user clarifications. If a
  handoff changes the agreed scope, stop and confirm the change with the user.
  Challenge implementation policies that weaken or replace the user's intended
  experience; an author's rationale or passing tests do not authorize that change.

## Acceptance for each ticket

- Review correctness, design, regressions, resource usage, and relevant edge
  cases. Trace the complete relevant workflow across component boundaries.
- Run associated tests and assess whether they actually prove the requirements.
  Derive expected behavior independently from the agreed requirements. Checks
  that restate implementation choices can pass while preserving the defect.
  Add focused independent checks where existing coverage assumes away likely
  failures, including negative cases that challenge the proposed solution.
  Follow the test coordination rules above.
- Data-dependent features require realistic data and the user's actual library
  where applicable. Synthetic fixtures alone are insufficient for accepting
  complex data behavior.
- For preparation and recommendation changes, check fresh-user startup,
  incomplete processing, missing metadata, continuing updates, and time to useful
  output. Existing prepared state must not conceal startup dependencies.
- For UI changes, inspect the rendered result and exercise relevant interactions.
  Establish which code the running application actually loaded; a changed source
  file does not prove that the running app uses it.
- Judge the whole rendered composition against the agreed visual direction and
  interaction model. Check legibility, label association, hierarchy, clutter,
  and preservation of meaningful artwork at relevant screen sizes. Coordinate,
  containment, element-count and overflow assertions do not establish visual
  quality. Inspect screenshots rather than merely confirming they were produced.
- For progression and unlock interfaces, trace preparation conditions, required
  predecessors, map routes, notices, navigation and destination access together.
  They must express the same agreed progression. Distinguish usable output,
  unlocked access, successful exploration and notification visibility; preserve
  the agreed independence of optional branches.
- Check downstream output completing before its required predecessor. It must
  not bypass the agreed unlock order. Verify that illustrated routes follow the
  artwork and represent meaningful progression; reconnecting a ready destination
  to another ready ancestor does not prove the intended prerequisites hold.
  Understand actual readiness conditions before accepting the map's layout or
  ordering. Require artwork or layout changes when needed to express them.
- Inspect the user's database through read-only access or an isolated snapshot.
  Never run tests or migrations against it to obtain review evidence.
- Record the tested requirements, commands, results, dataset, runtime version,
  and remaining gaps in the review comment. Clearly distinguish independent
  observations from author reports and identify any use of controlled fixtures.
  Match each acceptance claim to evidence that establishes it. Actual-library
  readiness establishes usable content, not visual quality or correct progression.
  After revisions, identify which earlier evidence remains applicable and which
  affected requirements need renewed verification.

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

- Keep actively reviewing while any ticket remains In review. Work through its
  outstanding code review, tests, experiments, UI checks and acceptance evidence.
  Pause for a ten-minute queue check only when no tickets remain In review.
  Do not end monitoring merely because the queue is temporarily empty. Keep
  waits interruptible and remain responsive to user instructions.
- Continue code review, focused checks and experiments while another agent owns
  the global-suite lock. Do not launch another global suite or bypass its lock.
- Run the full suite after the current integration list is complete, subject to
  the user's current instructions. An empty review queue alone does not establish
  that deferred integration work is complete.
- File newly discovered test failures in Ready with exact test names, commands,
  results, diagnostics, and an appropriate priority. Check for existing failure
  tickets before creating duplicates.
- Keep review evidence and outstanding acceptance checks in ticket comments so
  the next reviewer can establish what remains. Keep changing ticket state out
  of these permanent instructions.
