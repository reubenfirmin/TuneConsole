# Retained-data restore experiment for #658

This is an experiment, not the production compactor. The source is the current
native database, opened read-only. First make a complete SQLite backup and copy
its referenced files. Build a new database from that backup. Leave the native
database and application configuration in place. The user will decide when to
start the application against the experimental database.

## Restore contract, version 1

Use the source's actual installed schema, including indexes, views, triggers,
applied migration history, IDs, implicit row IDs, sequence high-water marks and
SQLite application/user versions. Do not pretend historical preparation has
completed, run historical migrations, or change feature/model version settings.

Keep every row in every table except the explicitly filtered owners below.
The generated `restore-plan.json` lists **every source table**, its exact SQL
predicate, source/retained counts and the retained key sets. It is written before
creating or filling the candidate. Any foreign-key dependency adds its parent;
it cannot silently become a missing reference.

The unchanged owners therefore include all of the following:

- Accounts, canonical identities, catalog entries, recording/video identifiers,
  artist credits, album relationships, library and playlist membership, playlist
  observation/change times, saved recipes, manual settings and corrections.
- Exact and coarse listening history, player events, generated-play provenance,
  ratings, feedback, served impressions, responses, offer contexts, model
  comparisons, checkpoints, policy definitions and group evidence roots.
- Model training inputs and existing published output: recording/artist vectors,
  content encoders and fingerprints, taste modes/axes, learned weights,
  selection-run history, musical source/profile state, discovery candidates and
  current prepared recommendations. Preserve pending work and its versions;
  report existing backlog separately from work caused by this restore.
- **Vibe storage follows [#481](https://github.com/reubenfirmin/TuneConsole/issues/481):**
  sampled attribute payloads stay in external JSON files. `sample_attributes`
  stores collection identity and a filename, with no attribute payload. SQL
  retains scores/history in `measurements`, `predictions` and `recording_vibes`,
  and relationships in `measurement_samples`, `prediction_artists` and
  `prediction_inputs`. The latter contains only `(prediction_id, measurement_id)`
  links, not copies of model input attributes. Preserve all these rows, plus
  playback runs, human development annotations and their relations/files.
  Preserve exact external input bytes, scores, support, contributor IDs,
  model/policy versions and current selections; do not import JSON payloads
  into SQL during restoration.
- Provider state, attempts and attempt fields, capture/selection heads, metadata
  conflicts/candidates, hints and native musical-source observations. Retaining
  these avoids presenting already-completed provider work as missing.
- Recovery archives, action/undo state, unresolved evidence, durable publication
  and preparation state. A missing selection does not erase personal evidence.

## Explicit row filters

1. **Recommendation generations and pools.** Use the existing
   `retained_copy.plan` domain rules: preserve current heads, explicit references,
   live builds and any additional FK owner. Copy the dependent outputs,
   candidate rows, surfaces, bands and framing for those roots. Do not truncate
   candidate pools or remove low-volume eligible genres.
2. **Musical groups.** Start with the existing group-retention closure: current
   head, evidence generations/versions, retained recommendations, live builds,
   first alias publications and required historical lineage. Preserve complete
   selected generation memberships, assignments, representatives, graph rows,
   manifests and input records. Expand parent/child closure until stable; do not
   drop a referenced alias or an ancestor merely to reach a size target.
   Scratch retirement-walk tables are emptied; they are resumable cleanup work,
   not learning evidence. Keep the roots-complete flag and epoch unchanged.
3. **`metadata_observations`.** Keep all non-legacy observations, all
   `legacy_choice` observations, every observation referenced by a retained
   owner, and the complete `derived_from_observation_id` ancestry. Other
   historical legacy logs/findings/attempt expansions remain in the complete
   backup rather than the experimental database. This is a deliberate
   experimental history omission; it is not approval for production deletion.
4. **`metadata_legacy_records`.** Keep records referenced by retained
   observations and records belonging to retained deliveries. Preserve the
   original legacy owner/ID/disposition without inventing provenance.
5. **`metadata_deliveries`.** Keep all non-legacy deliveries and every delivery
   referenced by retained observations, native source evidence, provider state,
   attempts, artist credits, captures, identifiers or retained legacy records.
   Only unreferenced legacy expansions are left out of the experimental DB.

All FK parent closure is mandatory. If it keeps more history than expected, the
report must expose that cost instead of silently weakening these rules.

## Files and backup custody

The full backup contains the unchanged SQL snapshot, exact bytes of every
referenced file, an original-path mapping, SHA-256 hashes, file sizes and the
source schema/sequence metadata. Include sample and annotation files, policies,
group definitions/metadata/lineage packs, prepared recommendation documents,
metadata response packs, descriptions, collection feedback, legacy archives,
action backups, selected preparation files and nested preparation-plan pages.
Also preserve the recommendation evidence ledger and deferred playback inbox
when present. Do not copy credentials into reports.

The candidate gets private copies of its referenced files. Rewrite only the
candidate's file paths, keeping every referenced byte range, digest and offset
unchanged. Whole immutable packs may contain unselected ranges: report their
physical size honestly. Keep the original backup and its files untouched.

## Measurements and checks before startup

- Record backup, planning, schema, copy, index, file-copy and verification times
  separately, plus SQL/index/file logical and allocated sizes.
- Check SQLite integrity, foreign keys, exact retained row values/types/IDs,
  preserved sequences/schema/history, referenced file bytes and source custody.
- Compare training baskets, listening counts, current model/version settings,
  current vibe scores and selected recommendation/group heads with the backup.
- Record pending queues and maintenance eligibility before startup so existing
  unfinished work is distinguishable from restore-induced work.
- Keep a pristine candidate as well as the future startup copy. App startup and
  ongoing writes must not alter the backup or the pristine comparison baseline.

Startup observations, any failing assumption and later contract revisions belong
in the experiment report and #658. A small file alone does not establish success.

## Running this experiment

The October 31 experiment lives in `backups/658-restore-experiment-20261031`.
Its `RESULTS.md` records measurements, existing backlog and remaining checks.
`original.db` and `backup-files` are the complete recovery point. `candidate`
is the pristine retained baseline. `startup` is a separate app home with private
file copies, a restored playback inbox, the full evidence ledger and the native
configuration copied into it. Only `startup` should be opened by the application.

From this checkout, the commands used to prepare it are:

```sh
.venv/bin/python experiments/retained_copy/run.py backup backups/658-restore-experiment-20261031 --source /home/rfirmin/.local/share/yt-playlist/state.db
.venv/bin/python experiments/retained_copy/run.py plan backups/658-restore-experiment-20261031
.venv/bin/python experiments/retained_copy/run.py build backups/658-restore-experiment-20261031
.venv/bin/python experiments/retained_copy/run.py baseline backups/658-restore-experiment-20261031
.venv/bin/python experiments/retained_copy/run.py stage backups/658-restore-experiment-20261031 --config /home/rfirmin/.config/yt-playlist/config.toml
```

These destinations already exist; do not repeat creation commands over them.
The experiment deliberately rejects existing backup/candidate/startup destinations.

When ready to observe startup, run:

```sh
YT_PLAYLIST_HOME="$PWD/backups/658-restore-experiment-20261031/startup" .venv/bin/python -m yt_playlist --host 127.0.0.1 --port 8765
```

Use an unused port if another instance is listening. This launches the checkout's
application against the experimental home. Its log is
`startup/logs/app.log`; it does not select the native database. Preserve
`baseline-report.json` when interpreting any work shown by the application.
