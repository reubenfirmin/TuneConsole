# Experiments

Keep research code, reproducible protocols and concise decision history here.
[Audio lab](audio_lab/README.md) covers audio datasets and vibe models;
[retained-copy experiments](retained_copy/README.md) cover storage and restore
contracts. `investigations/` contains executable diagnostic checks.

## Research records

- Keep one log per dataset: source/version, usable coverage, label meaning,
  important exclusions, experiments tried, conclusions and remaining gaps.
- Keep one provenance log per model: selected recipe, training inputs, frozen
  source/export identity, adoption decision, and rejected alternatives with the
  evidence explaining their rejection. Shared encoder comparisons belong in
  one encoder log, linked by the affected models.
- Record a decision once, with a dated result, a few decisive measurements and
  a reproducible command or evidence location. Update superseded conclusions;
  distinguish an experiment's result from a later product adoption decision.
- Keep full predictions, per-fold tables, source snapshots, receipts, timing
  transcripts and run logs in ignored research artifacts. Preserve failed runs
  there when they explain a result. Do not copy them into narrative source docs.
- Keep ticket descriptions, handoffs, review queues and progress diaries in
  tickets. Do not maintain repository copies or add per-ticket Markdown files.
- Preserve protocols and supporting documents whose exact bytes participate in
  frozen evidence hashes. They are reproducibility inputs, not growing logs.
  Define a new protocol for a changed experiment rather than rewriting history.

The audio logs summarize the previous reports. Historical prose remains in Git
history; detailed machine-readable evidence remains under the existing ignored
`audio_lab/local/` paths. Local evidence paths are relative to `audio_lab/` and
are not guaranteed to exist in a fresh checkout. A source archive or receipt
check establishes integrity, not model quality or independent acceptance.
