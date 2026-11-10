# Experiments

Keep research code, reproducible protocols and concise decision history here.
The [audio history](audio_lab/README.md) records the sequence of studies;
[retained-copy experiments](retained_copy/README.md) record storage measurements
and decisions. Current [storage contracts](../src/yt_playlist/core/storage-contracts.md)
live with the implementation. `investigations/` contains executable diagnostic checks.

## Audio research code

`audio_lab_tools/` is a Python package organized by responsibility:

| Package | Contents |
| --- | --- |
| `datasets` | Dataset discovery, acquisition, annotations and aligned windows |
| `audio` | Audio decoding, feature extraction, browser comparisons and encoder workers |
| `training` | Shared fitting, calibration, learner comparisons and model exports |
| `evaluation` | Metrics, cross-corpus transfer, identity exclusions and score contracts |
| `focus`, `cruise`, `release`, `relax`, `uplift`, `energize` | Each vibe's model recipes, exports and production replay checks |
| `pooling` | Library pooling, source assignment and recommendation evaluation |
| `replay` | Isolated library runtimes and downstream consumer comparisons |
| `studies` | Campaigns and candidate studies built on the shared tools |
| `support` | Evidence receipts, provenance, caches and research coordination |

Tests live with the corresponding tools. Package membership describes the code's
role; adoption and rejection decisions remain in the audio history.

Use `./experiments/run.py COMMAND` for the existing command names, and
`./experiments/run.py check` for focused research tests. `check --test FILENAME`
continues to accept test basenames. Direct module invocations include the package,
for example `uv run python -m experiments.audio_lab_tools.training.train_vibes --help`.

Historical source archives retain their original layout and hashes. Inspect those
archives to reproduce frozen runs; changed source requires a new run directory.

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
- Freeze experiment settings in machine-readable plans alongside their source
  snapshots. Historical documents required by old evidence hashes belong in
  ignored artifacts (`audio_lab/local/protocols/`), not separate tracked plans.
  Preserve those bytes; a changed experiment needs a new frozen plan.

The audio history summarizes the previous reports. Historical prose remains in Git
history; detailed machine-readable evidence remains under the existing ignored
`audio_lab/local/` paths. Local evidence paths are relative to `audio_lab/` and
are not guaranteed to exist in a fresh checkout. A source archive or receipt
check establishes integrity, not model quality or independent acceptance.
