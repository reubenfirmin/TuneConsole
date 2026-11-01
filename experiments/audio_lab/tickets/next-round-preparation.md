Parent: #530. Priority: **P2**. Related: #405 and #136.

Prepare the integration design and an isolated real-library replay for per-vibe
model updates. Existing compatible audio captures should be re-scored without
another audio extraction pass. Model changes must remain bounded and reversible.

Depends on the versioned artifact contract and independent-score decisions under
#530. Keep this phase under `experiments/`; evaluate an isolated database copy,
never run migrations or acceptance checks against the live database.

- Include the per-vibe artifact identity in prepared-result versions. Invalidate
  and refresh affected recording scores through the existing bounded background
  queue, preserving UI responsiveness and usable output during processing.
- Rebuild derived and propagated results from original measurements. Stored
  predictions never become extra independent measurement votes or human labels.
- Cover missing/incompatible measurements, one partial capture, later excerpts,
  missing metadata, repeated delivery, model rollback and stale cache invalidation.
- Establish per-vibe fallback and rollback behavior; retain current behavior for
  unaffected vibes. Do not let a failed optional head discard all usable evidence.
- Replay the actual library with fresh and partially prepared states under the
  normal preparation budget. Record time to useful output and complete Mix Radio
  results; an extended-time diagnostic does not establish normal startup success.

Acceptance: reviewable integration specification and reproducible isolated
replay with actual library data, explicit runtime versions and resource results.
Production edits are a later implementation step after the experiments-only
boundary is lifted.
