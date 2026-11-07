# Experiments

Keep research code, reproducible protocols and concise decision history here.
The [audio history](audio_lab/README.md) records the sequence of studies;
[retained-copy experiments](retained_copy/README.md) record storage measurements
and decisions. Current [storage contracts](../src/yt_playlist/core/storage-contracts.md)
live with the implementation. `investigations/` contains executable diagnostic checks.

## Research records

- Keep [dataset references](audio_lab/datasets/README.md) separate from experiment
  history. Define the collection, native labels and scales, supplied audio,
  source/version URLs, and the subset used here. Link publisher releases and
  exact acquired mirrors. Keep dated results, ticket narration and run diaries
  out of dataset pages; ignored artifact paths are not dataset source URLs.
- Keep the audio history in one chronological document, one paragraph per study:
  question/recipe, decisive result, adoption or rejection, and a reproducible
  command or evidence location. Include dataset coverage or exclusions when they
  affect the conclusion. Keep exact acquisition and model provenance in the
  referenced artifacts and bundled model metadata.
- Record a decision once. Update superseded conclusions and distinguish an
  experiment's result from a later product adoption decision. Keep setup and
  current runtime behavior in the owning extension or recommendation reference.
- Keep full predictions, per-fold tables, source snapshots, receipts, timing
  transcripts and run logs in ignored research artifacts. Preserve failed runs
  there when they explain a result. Do not copy them into narrative source docs.
- Keep ticket descriptions, handoffs, review queues and progress diaries in
  tickets. Do not maintain repository copies or add per-ticket Markdown files.
- Preserve protocols and supporting documents whose exact bytes participate in
  frozen evidence hashes. They are reproducibility inputs, not growing logs.
  Define a new protocol for a changed experiment rather than rewriting history.

The audio history summarizes the previous reports. Historical prose remains in Git
history; detailed machine-readable evidence remains under the existing ignored
`audio_lab/local/` paths. Local evidence paths are relative to `audio_lab/` and
are not guaranteed to exist in a fresh checkout. A source archive or receipt
check establishes integrity, not model quality or independent acceptance.
