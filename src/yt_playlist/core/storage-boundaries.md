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
| Home, listening, models and reports | `controllers/home_outputs.py`, `controllers/listening_analytics.py` and `controllers/recommendation_proposals.py` coordinate publication. `core/home_outputs.py`, `core/listening_outputs.py` and `core/recommendation_reports.py` supply preparation, validation and registration functions. `controllers/settings.py` coordinates recommendation setting outputs with `repos/settings.py`. These owners use `repos/recommendation_file_writes.py` for SQL receipts and `core/recommendation_files.py` for bodies. Generation and revision checks remain with the publishing owners. |
| Descriptions and notes | `controllers/album_descriptions.py` and `controllers/album_notes.py` coordinate requests and publication. `repos/album_descriptions.py` and `repos/album_notes.py` own SQL. `core/album_description_files.py` and `core/recommendation_files.py` handle the bodies. |
| Audio samples and annotations | `controllers/audio_capture.py` interprets capture inputs and `controllers/audio_vibes.py` coordinates publication. `repos/audio_vibes.py` owns SQL and `core/sample_files.py` stores excerpt bodies. `core/audio_annotations.py` stores and exports complete judgment revisions from SQLite. |
| Policies, groups, recommendations and preparation documents | `controllers/policies.py`, `controllers/group_publication.py` and `controllers/recommendation_publications.py` coordinate domain writes over their SQL repositories and the `PolicyFiles`, `GroupFiles` and `RecommendationFiles` ports. `controllers/recommendation_impressions.py` composes served-impression SQL with `core/served_files.py`. Runtime preparation and checkpoints stay with the feature owners. Playlist cleanup attempts are stored on playlist scopes by account. Spotify transfer receipts use `controllers/workflow_settings.py` with settings SQL and the collection file port. |
| Profiles, embeddings, Vibe and historical retention | `controllers/musical_profiles.py`, `controllers/embeddings.py`, `controllers/vibe_models.py` and `controllers/vibe_predictions.py` own runtime publication with their SQL repositories and the `ProfileFiles`, `EmbeddingFiles`, `VibeModelFiles` and `ProvenanceFiles` ports. Prediction retention uses the rules in `core/vibe_history.py`. Explicit profile and embedding batch helpers remain, as described below. Existing policy-history maps and legacy archives/journals retain their installed readers and recovery references. |
| Choice evidence and playback inbox | `controllers/evidence_ledger.py` uses `repos/evidence_files.py` and `core/evidence_record_files.py`. `core/evidence_contract.py` and `core/evidence_retirement.py` own validation and retirement rules. `controllers/playback_inbox.py` uses the FAO-managed JSON queue in `core/playback_inbox_file.py`. Replay commits local projections and their receipts together in the library database before acknowledging the JSON capture. |

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
are separate from ordinary startup and schema upgrades.

## Runtime retention

Managed-file and collection retention keep the explicit controllers, SQL queries
and database-free candidate/file ports composed in
`library/retention_composition.py`. Current ownership, file identity, reader
fences and generation/revision predicates govern final deletion. No reporting
transaction or cross-repository recovery protocol encloses these owners.
