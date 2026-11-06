# Database maintenance

## Startup and migrations

Fresh databases initialize directly. Supported installed schemas receive
structural checks and bounded regular changes; startup does not replay the
historical bulk-conversion chain. Released v0.1.18/schema-64 libraries and the
supported development schema 93 have explicit background rebuild importers.
Other installed-layout admission is defined in `schema_admission.py`.

`migrations/regular.py` owns additive changes and the `schema_changes` ledger.
`migrations/registry.py` retains historical definitions and `schema_migrations`.
Each change and its stamp commit together. Never stamp unfinished preparation
as an applied migration or edit an applied historical definition.

```sh
uv run python -m yt_playlist.core.migrations upgrade-schema /path/to/state.db
uv run python -m yt_playlist.core.migrations audit /path/to/state.db
```

`upgrade-schema` applies regular changes only. `audit` checks a committed
snapshot read-only; it does not repair data or run preparation.

## Resumable preparation

Preparation commands below write to an existing installed library. Back up the
database and its owned files before operational maintenance. Use isolated
copies for experiments and tests.

Preparation retains installed sources, exact originals and committed checkpoints.
Continuing writes invalidate captured revisions; reconcile before publishing.
Publication verifies complete inputs outside the writer transaction, then
selects immutable heads with a short revision check. Existing readers retain
their snapshots. Native owners take precedence when installed.

Jobs normally accept `--status`, `--batches N`, `--batch-size N` and explicit
`--retry`. Limits are owner-specific; inspect the module's `--help` for supported
flags and bounds. Failed jobs retain earlier commits. Publication does not
advance historical migrations or authorize deleting source owners.

Example:

```sh
uv run python -m yt_playlist.core.preparation.listening_subjects /path/to/state.db --status
uv run python -m yt_playlist.core.preparation.listening_subjects /path/to/state.db --batches 32 --batch-size 32
uv run python -m yt_playlist.core.preparation.subject_publication /path/to/state.db --batches 32 --batch-size 32
uv run python -m yt_playlist.core.preparation.subject_publication /path/to/state.db --publish-stage STAGE_ID
```

Repeat bounded work until complete. Use the returned stage or prepared revision;
do not invent a head from row counts or assume the largest ID is current.

### Command index

All module names below follow `uv run python -m yt_playlist.core.preparation.`.
Where listed, the job argument comes before the database path. Inspect
`MODULE --help` for the publication selector: most use `--publish-stage`, while
document publishers use `--revision`.

| Data | Preparation module and job arguments | Publication module |
| --- | --- | --- |
| Listening subjects | `listening_subjects` | `subject_publication` |
| Metadata evidence | `metadata_evidence` | `metadata_publication` |
| Metadata conflict candidates | `metadata_conflicts` | `conflict_publication` |
| Metadata delivery/selection links | `metadata_destinations --stage STAGE_ID` | Same module, `--publish` |
| Scheduling, offer deduplication, album demand | `recommendation_state work`, `home-offers`, `album-requests` | `work_publication`, `offer_publication`; album requests feed note preparation |
| Recommendation demand | `recommendation_demand` | `demand_publication` |
| Genre preferences | `genre_preferences enabled`, `defaults` | `genre_preference_publication` |
| Genre classifications | `genre_classifications` | `genre_publication` |
| Taste axes | `taste_axes` | `taste_publication` |
| Ordered recipe memberships | `recipe_memberships` | `recipe_publication` |
| Listening display files and links | `listening_outputs files`, `links` | `listening_publication` |
| Home files and roots | `home_outputs files`, `roots` | `home_publication` |
| Recommendation reports | `recommendation_reports` | `report_publication` |
| Choice-learning inputs | `choice_learning` | `choice_publication` |
| Album notes | `album_notes` | `album_note_publication` |
| Radio checkpoint | `radio_session` | `radio_publication` |
| Album feedback/account authentication | `collection_feedback album-feedback`, `account-auth` | `collection_feedback_publication`, same job |
| Road Trip drafts | `road_trip_drafts` | `draft_publication` |
| Playback context | `playback_context bindings`, `pending-album` | `playback_publication`, same job |
| Radio source references | `radio_origins` | `radio_origin_publication` |
| Discovery checkpoints | `discovery_checkpoints fetch-history`, `artist-images`, `provider-refresh` | `discovery_checkpoint_publication`, same job |
| Library sync state | `library_sync result`, `stamps` | `library_sync_publication`, same job |
| Album controls/model pointers | `collection_controls album-controls`, `model-pointers` | `control_publication`, same job |
| Model checkpoints | `model_checkpoints content-checkpoints`, `group-checkpoints`, `candidate-checkpoints` | `checkpoint_publication`, same job |
| Saved encoders/fingerprints | `model_artifacts encoders`, `fingerprints` | `model_publication --artifact-revision A --fingerprint-revision F` |
| Trials/monitor/album assignment | `model_trials live-trial`, `listening-monitor`, `album-assignment` | `trial_publication`, same job |
| Radio seeds/autotune reports | `saved_outputs radio-seed`, `autotune-report` | `saved_output_publication`, same job |
| TV state | `tv_state aliases`, `pool`, `artwork`, `artwork-requests`, `visual-checks`, `continuations` | `tv_publication` for all except aliases |
| Ranked pairs and clicked rows | `ranked_state pairs`, `roots`, `clicked` | `pair_publication`, `clicked_publication` |
| Taste controls | `taste_controls checkpoint`, `baseline`, `decisions` | `taste_control_publication`, same job |
| Import/recipe/canvas documents | `collection_documents import`, `recipes`, `canvases` | `import_publication import`; `collection_document_publication recipes` or `canvases` |
| Recovery receipts | `workflow_receipts gc`, `playlists`, `albums` | `workflow_publication`, same job |
| Action ledger | `action_ledger` | `action_publication` |
| Append operations | `append_operations tv_legacy`, `radio_legacy`, `tv_native`, `radio_native` | `append_publication`, same job |
| Album tracklists/shares | `album_occurrences`, `album_shares` | `album_publication`, `album_share_publication` |
| Discovery subjects | `discovery_subjects rows`, `classifications` | `discovery_publication legacy` or `classified` |
| Discovery artists | `discovery_artists artists_legacy`, `artists_classified` | `discovery_artist_publication`, same job |
| Historical constraint audits | `domain_contracts OWNER`, `installed_genre_contract`, `frozen_shares` | `--verify` on the same module |
| Listening indexes | `calendar_indexes` | Same module, `--publish REVISION` |
| Pending deletion readback | `deletion_readback` | Preparation installs the guarded result |

### File preparation and repair

Managed-file conversion uses a separate domain runner:

```sh
uv run python -m yt_playlist.core.preparation /path/to/state.db --domain profiles --batches 10
uv run python -m yt_playlist.core.preparation /path/to/state.db --domain compression --batches 10 --assets MANAGED_ASSET_TREE
```

Other domains include `embeddings`, `vibe_provenance`, `legacy_journals` and
`policy_history`. Compression defaults to 32 MiB logical input per tick;
`--logical-bytes` may raise it up to 512 MiB. Additional roots must be explicit.

Some publishers can restore missing retained files from an unchanged source:
`conflict_publication --repair-conflict ID`,
`listening_publication --repair-source SURFACE`,
`collection_document_publication --repair-source PLAYLIST_SCOPE`,
`draft_publication --repair-recipe ID`, and
`album_note_publication --repair-files`. Check the command's source/digest
requirements first. Missing or corrupt required evidence is never an empty result.

## Rebuild and compaction

The running app exposes local maintenance endpoints:

| Request | Operation |
| --- | --- |
| `POST /storage-maintenance/rebuild` | Fresh current-schema rebuild |
| `POST /storage-maintenance/rebuild?resume=true` | Resume an eligible interrupted build |
| `POST /storage-maintenance/compact` | Retained-copy compaction in the installed format |
| `GET /storage-maintenance/status` | Progress and phase details |

For a stopped library:

```sh
uv run python -m yt_playlist.core.db_rebuild.job /path/to/state.db --scratch /existing/directory/with/space
```

Compaction preserves the installed format, migration history, row IDs, sequence
high-water marks and required file coordinates. It omits only disposable data
under the retention contract. A rebuild uses the declared source importer and
current destination schema. Neither operation replays historical conversions
against the serving source.

Construction serves reads while library writes pause. Playback and Radio remain
available; a durable inbox retains confirmed feedback and observations for local
replay. Replay never repeats remote rating or Next commands. Sync, imports and
other writes wait or defer. Cutover briefly fences requests and requires every
participating database handle to close.

The installer independently verifies SQL, file custody and integrity, then
installs a synced SQL sibling under exclusive admission. Managed file roots stay
in place. It never removes a live WAL. Source, candidate and verified recovery
backup survive failure; `state.db.compaction.json` records recovery coordinates.
Startup reconciles interrupted installation before opening the database.
Do not delete recovery copies until acceptance and a separate cleanup decision.

When manually copying a stopped library, include its managed files,
playback-feedback sidecar database, lock file and observation directory.
Capacity checks include SQL images, journals, owned files and a 256 MiB reserve
on each filesystem. Retained recovery copies still consume physical space.

## Ownership references

- [Schema owners and identity](../../../SCHEMA_OWNERSHIP.md)
- [Field invariants](../../../SCHEMA_INVARIANTS.md)
- [Managed file storage](../../../experiments/retained_copy/FAO-OWNERS.md)
- [Retained-copy experiment](../../../experiments/retained_copy/README.md)
- [Recommendation publication](../rec/README.md)
