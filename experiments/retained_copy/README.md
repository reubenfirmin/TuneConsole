# Retained-copy experiment history

Current [storage contracts](../../src/yt_playlist/core/storage-contracts.md) and
[maintenance commands](../../src/yt_playlist/core/README.md) live with the code.
[Managed file ownership](FAO-OWNERS.md) remains the file-format reference.
The studies below used isolated actual-library copies. Evidence paths are relative
to the repository root, ignored by Git and unavailable in a fresh checkout.
Measurements describe those runs; they do not establish current UI latency or
authorize changes to the native library.

## 31 October 2026: retained restore

The first restore tested whether domain retention could shrink schema 93 without
changing installed schema/history, IDs, model inputs or current selections. It
retained referenced recommendation/group closures and personal metadata choices,
omitting only unreferenced legacy metadata expansions in the experimental copy.
SQL fell from 2,752,774,144 to 1,221,394,432 bytes (55.6%); 19,700 files added
400,001,304 logical bytes and the evidence ledger 97,540,522 bytes. Equivalence
checks passed, but the roughly 1.72 GB logical result missed the few-hundred-MB
goal. This established a retained baseline for owner-specific studies; native-app
startup, continued writes and installation were separate outstanding checks.
Evidence: `backups/658-restore-experiment-20261031/RESULTS.md`; reproducer: [run.py](run.py).

## 1 November 2026: runtime metadata

This study separated current catalog/scheduling facts from provider alternatives,
manual decision history and native input documents, then evaluated retirement of
empty historical locators. The retained baseline's SQL fell from 1,221,394,432 to
808,685,568 bytes; 40,228 runtime files used 48,535,229 logical and 181,100,544
allocated bytes. Complete baseline comparisons found no differences. A separate
continued-write exercise accepted 100 provider results without SQL page growth
and preserved manual override/reset after reopening. The result favored compact
current SQL with owned files, while retaining explicit activation and independent
recovery verification; schema opening was measured separately from app startup.
Evidence: `backups/681-682-runtime-experiment-20261101-final/`;
reproducer: [runtime_metadata.py](runtime_metadata.py).

## 1 November 2026: served facts

Moving diagnostic provenance into owned files left captured reporting facts in SQL.
All 43,382 impressions produced identical scorecards before preparation, afterward
and after diagnostic expiry. The files consumed 57,831,143 logical and 184,291,328
allocated bytes; historical SQL provenance still awaited separate retirement, so
this did not establish net whole-database savings. The result supported reports
that remain independent of expiring diagnostics. Evidence:
`backups/687-served-facts-report.json`; reproducer: [served_facts.py](served_facts.py).

## 1 November 2026: mix choices

Frozen displayed descriptors replaced historical group pins as broad choice
evidence. Target-owner SQL/index allocation fell from 292,913,152 to 9,322,496
bytes, with two repacked group files totaling 440,970 bytes. All 437 current
groups, 25,225 memberships, 117 menus and 1,539 response events remained equal;
late and repeated writes worked. There were 183 descriptors sharing 163 genre
sets and 13 legacy options with incomplete provenance, excluded from preference
learning. This supported releasing obsolete group roots after projection without
inventing track-level precision. Evidence: `backups/688-mix-choices-report.json`;
reproducer: [mix_choices.py](mix_choices.py).

## 1 November 2026: credit and mode history

Permanent delivery replay receipts allowed obsolete artist captures to retire;
explicit tombstones allowed inactive taste-mode payloads to shrink while IDs and
labels survived. After simulated 31-day expiry and compaction, targeted SQL/index
allocation fell by 72,364,032 bytes with no external files added. Fourteen active
modes remained byte-identical, selected artist projections matched, and PPR ranks
matched for 2,800 items. Replay after expiry preserved original outcomes; 2,172
inactive mode payloads retired. This supported bounded explicit retention rather
than another cold archive. Evidence: `backups/691-698-runtime-history-report.json`;
reproducer: [runtime_history.py](runtime_history.py).

## Vibe version normalization

The companion [vibe_storage.py](vibe_storage.py) study isolates normalization of
three repeated version strings into immutable IDs, retaining exact decoded
versions, original measurements, samples and current outputs. Its protocol also
separates 180-day superseded-summary retention from 30-day contributor details.
The previous note recorded the contract without a dated measurement report;
no additional savings or runtime acceptance is claimed here.

## Reproducing a study

For owner studies, run `uv run python -m experiments.retained_copy.MODULE
SOURCE.db NEW_REPORT.json`; `runtime_metadata` instead takes a new output
directory. Inspect the selected module's `--help` first. Use isolated sources
with their owned files available and new report destinations.

The restore prototype uses `uv run python -m experiments.retained_copy.run` with
`backup`, `plan`, `build`, `baseline` and `stage` in that order. `backup` takes
`--source SOURCE.db`; `stage` takes `--config CONFIG.toml`. All actions take the
same new experiment directory. Backup/candidate/startup creation rejects existing
destinations. Keep the original and pristine candidate unchanged; any app startup
uses only the separately staged home and its baseline report.
