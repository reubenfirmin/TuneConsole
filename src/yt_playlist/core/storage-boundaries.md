# Storage boundaries

Feature controllers own complete commands. SQL repositories supply detached
facts, committed file IDs and guarded head changes. File ports receive paths,
bytes or receipts. Codecs and plans interpret supplied values without discovering
SQL state or opening files. The [runtime owner inventory](runtime-file-owners.md)
defines record IDs, paths, pack limits and the pending/write/ready convention.
Connection-bound SQL adapters reuse the connection's existing serving lock,
including after rebinding or reopening. Plain offline connections stay owned by
their calling preparation/recovery command.

## Source families

Paths are relative to `src/yt_playlist`.

| Family | Current owners and disposition |
| --- | --- |
| Action and recovery | `controllers/actions.py`, `controllers/action_documents.py` and `controllers/workflow_settings.py` coordinate action and workflow writes. Action/settings repositories and `repos/collection_file_writes.py` own SQL receipts. `core/workflow_settings.py` interprets supplied values and SQL facts. `core/collection_files.py` and `core/action_backup_files.py` own file access. `core/collection_recovery.py` wires backup and restore through `controllers/collection_recovery.py`, `repos/collection_recovery.py`, `core/collection_backup.py` validation and `core/collection_backup_files.py`. |
| Metadata | `controllers/metadata_deliveries.py`, `controllers/metadata_choices.py` and `controllers/enrichment_state.py` coordinate acquisition and publication. `repos/delivery_file_writes.py` and `repos/metadata_file_writes.py` reserve and complete SQL receipts. `core/metadata_delivery_files.py` and `core/metadata_runtime_files.py` handle detached references and file bodies. |
| Home, listening, models and reports | `controllers/home_outputs.py`, `controllers/listening_analytics.py`, `controllers/comparison_outputs.py` and `controllers/recommendation_proposals.py` coordinate publication. `core/home_outputs.py`, `core/listening_outputs.py`, `core/model_projections.py` and `core/recommendation_reports.py` supply preparation, validation and registration functions. `controllers/settings.py` coordinates recommendation setting outputs with `repos/settings.py`. These owners use `repos/recommendation_file_writes.py` for SQL receipts and `core/recommendation_files.py` for bodies. Generation and revision checks remain with the publishing owners. |
| Descriptions and notes | `controllers/album_descriptions.py` and `controllers/album_notes.py` coordinate requests and publication. `repos/album_descriptions.py` and `repos/album_notes.py` own SQL. `core/album_description_files.py` and `core/recommendation_files.py` handle the bodies. |
| Audio samples and annotations | `controllers/audio_capture.py` interprets capture inputs and `controllers/audio_vibes.py` coordinates publication. `repos/audio_vibes.py` and `repos/annotation_file_writes.py` own SQL, with `core/sample_files.py` and `core/audio_annotation_files.py` handling the bodies. Immutable sample identity and analysis validation remain domain rules. Pending receipts stay invisible until completion. |
| Policies, groups, recommendations and preparation documents | `controllers/policies.py`, `controllers/group_publication.py` and `controllers/recommendation_publications.py` coordinate domain writes over their SQL repositories and the `PolicyFiles`, `GroupFiles` and `RecommendationFiles` ports. `controllers/recommendation_impressions.py` composes served-impression SQL with `core/served_files.py`. Runtime preparation and checkpoints stay with the feature owners. Playlist GC and transfer receipts use `controllers/workflow_settings.py` with settings SQL and the collection file port. |
| Profiles, embeddings, Vibe and historical retention | `controllers/musical_profiles.py`, `controllers/embeddings.py`, `controllers/vibe_models.py` and `controllers/vibe_predictions.py` own runtime publication with their SQL repositories and the `ProfileFiles`, `EmbeddingFiles`, `VibeModelFiles` and `ProvenanceFiles` ports. Prediction retention uses the rules in `core/vibe_history.py`. Explicit profile and embedding batch helpers remain, as described below. Existing policy-history maps and legacy archives/journals retain their installed readers and recovery references. `core/db_rebuild/references.py` and `core/db_rebuild/files.py` discover and verify those files during maintenance. |
| Choice evidence and playback inbox | `controllers/evidence_ledger.py` uses `repos/evidence_files.py` and `core/evidence_record_files.py`. `core/evidence_contract.py` and `core/evidence_retirement.py` own validation and retirement rules. `controllers/playback_inbox.py` uses `repos/playback_inbox.py` and `core/playback_observation_files.py`. Inbox receipts use an independent database and never join the library's SQL transaction. |

The [supported release baseline](README.md#startup-and-migrations) defines the
upgrade floor. Numbered migration history remains immutable metadata. Bounded
regular upgrades preserve applied history and older installations follow the
documented reinitialization path. Supported installed owners continue serving
reads and writes according to their format. New runtime files follow the
record-ID convention in the runtime owner inventory.

## HTTP content

`web/fao_response.py` adapts FAO `Content` to Starlette delivery. It owns the
materialization lease through normal completion, disconnect or cancellation.
It streams bytes instead of handing a temporary path to a server after the
response returns. Range delivery, logical cache headers and compression remain
supported. `web/fao_static.py` composes this adapter; `core/fao.py` owns mechanical
read/write, prepared publication, deletion and content materialization.

## Preparation commands

Runtime preparation is scheduled by `rec/progressive.py`. It receives bounded
source commands from `library/preparation_composition.py` and publishes through
feature controllers and their SQL and file capabilities. The storage CLI in
`core/migrations/__main__.py` frames connections and results for read-only audit
and bounded regular upgrade. The pre-release preparation CLI and staged
workflow and feedback conversion commands were retired. Connection-ownership
tests still exercise the SQL adapter in `repos/workflow_preparation.py`.

`prepare_batch()` in `core/profile_files.py` and `core/embedding_files.py` wires
the surviving installed-format helpers. `controllers/profile_preparation.py`
and `controllers/embedding_preparation.py` coordinate bounded batches with the
SQL-only source adapters in `repos/profile_file_references.py` and
`repos/embedding_file_references.py` and the detached file ports. Profile
cursors wrap to find rollback survivors and continuing legacy writes. Embedding
batches verify float32 bytes, vector basis and insertion order. These helpers
are separate from ordinary startup, upgrades and the current rebuild.

## Maintenance and rebuild

`core/storage_maintenance.py` owns background admission, cancellation and
progress. `core/db_rebuild/job.py` coordinates compaction and normalized
rebuild. `core/db_rebuild/cli.py` frames the explicit compaction command and its
connection. The job composes the following capabilities in `core/db_rebuild/`.

- `references.py` discovers SQLite schema, references and reader roots. Legacy
  file-map heads and evidence-directory ownership become detached descriptors;
  discovering them performs no file traversal or decoding.
- `retention_plan.py` derives retained-copy selection from supplied schema facts.
  `build.py` executes those private TEMP plans and constructs SQLite candidates
  with guarded, advancing bookmarks. Unknown FK owners protect their parents.
- `files.py` expands file descriptors, inspects/verifies files, holds reader
  fences, borrows roots and performs installed-file custody changes.
- `source.py` and `destination.py` admit supported sources, define bounded
  copy stages for the current schema and verify retained rows and history.
  `build.py` checkpoints stage progress and validates unchanged sources and
  destination versions on resume. Optional stage preparation runs before
  bookmark writer admission and remains leased through commit.
- `install.py` owns closed-file cutover, handle reopening and recovery after
  an interrupted installation. Startup recovery discovers batches of SQLite
  descriptors before handing each batch to file verification. Supported file
  roots stay available under the reader fences through cutover.

Managed-file and collection retention keep the explicit controllers, SQL queries
and database-free candidate/file ports composed in
`library/retention_composition.py`. Current ownership, file identity, reader
fences and generation/revision predicates govern final deletion. No reporting
transaction or cross-repository recovery protocol encloses these owners.
