#!/usr/bin/env python3
"""Compile the current destination definition outside application startup.

This maintenance build constructs an empty schema in an isolated directory. It
does not open an installed database or run a populated historical upgrade. The
result contains the frozen numbered contract, complete optimized destination
DDL, initial SQL state, and the exact two histories represented by that state.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'src/yt_playlist/core/schema'
FORMAT = 1


def render(schema):
    """Carry the frozen release baseline alongside the compiled destination."""
    return {path: path.read_text(encoding='utf-8')
            for path in (schema.CURRENT_DEFINITION, schema.CURRENT_SEEDS)}


def _objects(conn):
    # Tables precede views and guards. Insertion happens before guards, so seed
    # construction does not replay invalidation/capture side effects.
    return [sql for sql, in conn.execute(
        "SELECT sql FROM main.sqlite_schema WHERE sql IS NOT NULL "
        "AND name NOT LIKE 'sqlite_%' ORDER BY CASE type "
        "WHEN 'table' THEN 0 WHEN 'view' THEN 1 WHEN 'index' THEN 2 ELSE 3 END,name")]


def _seeds(conn):
    # SQLite's dumper owns literal escaping, including blobs, newlines and
    # quoted identifiers. Explicit row IDs reconstruct AUTOINCREMENT sequences;
    # its internal sqlite_sequence statements must not become application DDL.
    # History timestamps belong to the caller's transaction, not this build.
    omitted = ('INSERT INTO "schema_migrations"',
               'INSERT INTO "schema_changes"',
               'INSERT INTO "sqlite_sequence"')
    return [sql for sql in conn.iterdump()
            if sql.startswith('INSERT INTO ') and not sql.startswith(omitted)]


def compile_definition():
    # Set the isolated home before importing schema owners. None of their file
    # locators may resolve to the user's installed application directory.
    previous_home = os.environ.get('YT_PLAYLIST_HOME')
    try:
        with TemporaryDirectory(prefix='tuneconsole-schema-definition-') as workspace:
            os.environ['YT_PLAYLIST_HOME'] = workspace
            from yt_playlist.core import destination_schema
            from yt_playlist.core.migrations import schema_definition as schema
            from yt_playlist.core.migrations import (
                account_binding_receipt, account_bindings, action_collection_storage, album_playback_storage, album_storage,
                artist_credit_replay_storage, artist_details, discovery_artists, discovery_checkpoints,
                discovery_impression_storage, discovery_tracks, experiment_decision_storage, group_checkpoint_storage, group_context_owners,
                group_evidence_references, group_evidence_settings, group_generation_storage,
                group_input_digests, group_input_retention_storage,
                group_preview_evidence_storage, group_versions_by_generation,
                held_recommendation_state, history_worker_storage, home_storage, home_taste_settings, import_and_fit_receipts, maintenance_clocks,
                metadata_capture_storage, metadata_choice_storage, metadata_conflict_storage, metadata_delivery_storage, metadata_hint_storage, metadata_history_owners, metadata_observation_storage, metadata_selection_owners, mix_menu_storage, runtime_choice_owners,
                legacy_mode_storage, mix_worker_storage, model_artifacts_storage, model_selection_storage,
                paid_access_owner, paid_description_storage, play_event_storage, playback_binding_storage, playback_preference_storage,
                playlist_bookkeeping, playlist_items_storage, policy_archive_storage, policy_body_settings, policy_history_settings, policy_worker_storage, prediction_storage,
                home_recommendation_roots,
                prepared_display_files, prepared_display_settings, prepared_input_storage, radio_discovery_details, radio_launch_storage,
                playback_response_receipts, radio_policy_storage, rating_action_events, recommendation_build_state, recommendation_candidate_storage, recommendation_chunk_receipts, recommendation_custody, recommendation_membership, recommendation_receipt_storage,
                recommendation_refresh_storage, report_part_storage, track_association_history,
                choice_evidence_storage, choice_learned_settings, content_work_settings, control_settings_storage,
                embedding_storage, listener_output_storage, playlist_document_storage,
                mood_taste_storage, preparation_checkpoint_storage, provider_field_storage, publication_manifests, radio_tv_settings, shared_file_receipts,
                musical_observation_storage, musical_source_profile_storage, musical_source_queue_storage,
                recommendation_storage, recording_association_storage, recording_content_work,
                recording_profile_storage, recording_redirect_storage, regular, road_trip_storage, served_impression_storage,
                spotify_records_storage, spotify_storage, startup_worker_storage, sync_recovery_storage, sync_settings, taste_axis_graduations, taste_axis_storage, tv_draw_storage, tv_playlist_recovery, tv_preparation_details, tv_storage,
                vibe_model_receipts, vibe_worker_storage, work_sequences, worker_checkpoints,
            )
            from yt_playlist.core.migrations.registry import MIGRATIONS
            from yt_playlist.core.migrations.runner import Context, RELEASE_CHANGES

            if schema.VERSION != MIGRATIONS[-1].version:
                raise RuntimeError('The complete current definition must cover the production registry')
            with closing(sqlite3.connect(':memory:')) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute('PRAGMA foreign_keys=ON')
                conn.execute('BEGIN IMMEDIATE')
                schema.bootstrap(conn, MIGRATIONS, 0.)
                conn.execute(regular.HISTORY_SQL)
                for change in regular.CHANGES[:RELEASE_CHANGES]:
                    change.apply(conn, Context(0.))
                    change.validate(conn)
                    conn.execute('INSERT INTO schema_changes VALUES (?,?,?)',
                                 (change.version, change.name, 0.))
                conn.commit()
                # Keep the release layout for admission and compile the fresh
                # layout with the same changes that upgrade installed libraries.
                before_fold = next((index for index, change in enumerate(regular.CHANGES)
                                    if change.name == 'fold_track_sources'), len(regular.CHANGES))
                regular.migrate(conn, changes=regular.CHANGES[:before_fold], clock=lambda: 0.)
                installed_statements = _objects(conn)
                before_albums = next((i for i, change in enumerate(regular.CHANGES) if change.name == album_storage.NAME),
                                     len(regular.CHANGES))
                regular.migrate(conn, changes=regular.CHANGES[:before_albums], clock=lambda: 0.)
                album_predecessor_statements = album_storage.predecessor_objects(conn)
                before_plays = next((i for i, change in enumerate(regular.CHANGES)
                                     if change.name == play_event_storage.NAME), len(regular.CHANGES))
                regular.migrate(conn, changes=regular.CHANGES[:before_plays], clock=lambda: 0.)
                play_event_predecessor_statements = play_event_storage.predecessor_objects(conn)
                before_artists = next((i for i, change in enumerate(regular.CHANGES) if change.name == artist_details.NAME),
                                      len(regular.CHANGES))
                regular.migrate(conn, changes=regular.CHANGES[:before_artists], clock=lambda: 0.)
                artist_predecessor_statements = artist_details.predecessor_objects(conn)
                before_accounts = next((i for i, change in enumerate(regular.CHANGES) if change.name == account_bindings.NAME),
                                       len(regular.CHANGES))
                regular.migrate(conn, changes=regular.CHANGES[:before_accounts], clock=lambda: 0.)
                account_binding_predecessor_statements = account_bindings.predecessor_objects(conn)
                before_items = next((i for i, change in enumerate(regular.CHANGES)
                                     if change.name == playlist_items_storage.NAME), len(regular.CHANGES))
                regular.migrate(conn, changes=regular.CHANGES[:before_items], clock=lambda: 0.)
                playlist_item_predecessor_statements = playlist_items_storage.predecessor_objects(conn)
                before_predictions = next((i for i, change in enumerate(regular.CHANGES)
                                           if change.name == prediction_storage.NAME), len(regular.CHANGES))
                regular.migrate(conn, changes=regular.CHANGES[:before_predictions], clock=lambda: 0.)
                prediction_predecessor_statements = prediction_storage.predecessor_objects(conn)
                compact_owners = {
                    playback_preference_storage.NAME: ('playback_preference_predecessor_statements',
                                                       playback_preference_storage),
                    action_collection_storage.NAME: ('action_collection_predecessor_statements', action_collection_storage),
                    model_artifacts_storage.NAME: ('model_artifact_predecessor_statements', model_artifacts_storage),
                    musical_observation_storage.NAME: ('musical_observation_predecessor_statements',
                                                       musical_observation_storage),
                    tv_preparation_details.NAME: ('tv_preparation_detail_predecessor_statements', tv_preparation_details),
                    tv_playlist_recovery.NAME: ('tv_playlist_recovery_predecessor_statements', tv_playlist_recovery),
                    sync_settings.NAME: ('sync_settings_predecessor_statements', sync_settings),
                    spotify_records_storage.NAME: ('spotify_records_predecessor_statements', spotify_records_storage),
                    radio_discovery_details.NAME: ('radio_discovery_detail_predecessor_statements', radio_discovery_details),
                    radio_tv_settings.NAME: ('radio_tv_predecessor_statements', radio_tv_settings),
                    mood_taste_storage.NAME: ('mood_taste_predecessor_statements', mood_taste_storage),
                    home_taste_settings.NAME: ('home_taste_predecessor_statements', home_taste_settings),
                    provider_field_storage.NAME: ('provider_field_predecessor_statements', provider_field_storage),
                    shared_file_receipts.NAME: ('shared_file_receipt_predecessor_statements', shared_file_receipts),
                    choice_evidence_storage.NAME: ('choice_evidence_predecessor_statements', choice_evidence_storage),
                    choice_learned_settings.NAME: ('choice_learned_predecessor_statements', choice_learned_settings),
                    prepared_display_settings.NAME: ('prepared_display_predecessor_statements', prepared_display_settings),
                    content_work_settings.NAME: ('content_work_predecessor_statements', content_work_settings),
                    recording_content_work.NAME: ('recording_content_work_predecessor_statements', recording_content_work),
                    prepared_display_files.NAME: ('prepared_display_file_predecessor_statements', prepared_display_files),
                    home_recommendation_roots.NAME: ('home_recommendation_root_predecessor_statements', home_recommendation_roots),
                    group_context_owners.NAME: ('group_context_owner_predecessor_statements', group_context_owners),
                    group_evidence_settings.NAME: ('group_evidence_settings_predecessor_statements', group_evidence_settings),
                    legacy_mode_storage.NAME: (legacy_mode_storage.PREDECESSOR_KEY, legacy_mode_storage),
                    metadata_selection_owners.NAME: ('metadata_selection_owner_predecessor_statements', metadata_selection_owners),
                    runtime_choice_owners.NAME: ('runtime_choice_owner_predecessor_statements', runtime_choice_owners),
                    metadata_history_owners.NAME: ('metadata_history_owner_predecessor_statements', metadata_history_owners),
                    recommendation_chunk_receipts.NAME: ('recommendation_chunk_receipt_predecessor_statements', recommendation_chunk_receipts),
                    playback_response_receipts.NAME: ('playback_response_receipt_predecessor_statements', playback_response_receipts),
                    rating_action_events.NAME: ('rating_action_event_predecessor_statements', rating_action_events),
                    taste_axis_graduations.NAME: ('taste_axis_graduation_predecessor_statements', taste_axis_graduations),
                    recommendation_build_state.NAME: ('recommendation_build_predecessor_statements', recommendation_build_state),
                    recommendation_custody.NAME: ('recommendation_custody_predecessor_statements', recommendation_custody),
                    metadata_hint_storage.NAME: ('metadata_hint_predecessor_statements', metadata_hint_storage),
                    artist_credit_replay_storage.NAME: ('artist_credit_replay_predecessor_statements', artist_credit_replay_storage),
                    metadata_choice_storage.NAME: ('metadata_choice_predecessor_statements', metadata_choice_storage),
                    track_association_history.NAME: ('track_association_history_predecessor_statements', track_association_history),
                    playback_binding_storage.NAME: ('playback_binding_predecessor_statements', playback_binding_storage),
                    experiment_decision_storage.NAME: ('experiment_decision_predecessor_statements', experiment_decision_storage),
                    discovery_impression_storage.NAME: ('discovery_impression_predecessor_statements', discovery_impression_storage),
                    publication_manifests.NAME: ('publication_manifest_predecessor_statements', publication_manifests),
                    playlist_document_storage.NAME: ('playlist_document_predecessor_statements',
                                                      playlist_document_storage),
                    preparation_checkpoint_storage.NAME: ('preparation_checkpoint_predecessor_statements',
                                                          preparation_checkpoint_storage),
                    listener_output_storage.NAME: ('listener_output_predecessor_statements', listener_output_storage),
                    control_settings_storage.NAME: ('control_settings_predecessor_statements', control_settings_storage),
                    discovery_checkpoints.NAME: ('discovery_checkpoint_predecessor_statements', discovery_checkpoints),
                    discovery_tracks.NAME: ('discovery_track_predecessor_statements', discovery_tracks),
                    discovery_artists.NAME: ('discovery_artist_predecessor_statements', discovery_artists),
                    group_checkpoint_storage.NAME: ('group_checkpoint_predecessor_statements', group_checkpoint_storage),
                    group_evidence_references.NAME: ('group_evidence_reference_predecessor_statements', group_evidence_references),
                    group_input_digests.NAME: ('group_input_digest_predecessor_statements', group_input_digests),
                    group_versions_by_generation.NAME: ('group_versions_by_generation_predecessor_statements',
                                                        group_versions_by_generation),
                    recommendation_storage.NAME: ('recommendation_predecessor_statements', recommendation_storage),
                    recommendation_candidate_storage.NAME: ('recommendation_candidate_predecessor_statements', recommendation_candidate_storage),
                    recommendation_membership.NAME: ('recommendation_membership_predecessor_statements', recommendation_membership),
                    prepared_input_storage.NAME: ('prepared_input_predecessor_statements', prepared_input_storage),
                    report_part_storage.NAME: ('report_part_predecessor_statements', report_part_storage),
                    recommendation_receipt_storage.NAME: ('recommendation_receipt_predecessor_statements', recommendation_receipt_storage),
                    recommendation_refresh_storage.NAME: ('recommendation_refresh_predecessor_statements', recommendation_refresh_storage),
                    home_storage.NAME: ('home_predecessor_statements', home_storage),
                    metadata_delivery_storage.NAME: ('metadata_delivery_predecessor_statements', metadata_delivery_storage),
                    metadata_capture_storage.NAME: ('metadata_capture_predecessor_statements', metadata_capture_storage),
                    metadata_conflict_storage.NAME: ('metadata_conflict_predecessor_statements', metadata_conflict_storage),
                    metadata_observation_storage.NAME: ('metadata_observation_predecessor_statements', metadata_observation_storage),
                    mix_menu_storage.NAME: ('mix_menu_predecessor_statements', mix_menu_storage),
                    group_generation_storage.NAME: ('group_generation_predecessor_statements', group_generation_storage),
                    model_selection_storage.NAME: ('model_selection_predecessor_statements', model_selection_storage),
                    served_impression_storage.NAME: ('served_impression_predecessor_statements', served_impression_storage),
                    road_trip_storage.NAME: ('road_trip_predecessor_statements', road_trip_storage),
                    policy_archive_storage.NAME: ('policy_archive_predecessor_statements', policy_archive_storage),
                    policy_body_settings.NAME: ('policy_body_predecessor_statements', policy_body_settings),
                    policy_history_settings.NAME: ('policy_history_predecessor_statements', policy_history_settings),
                    recording_association_storage.NAME: ('recording_association_predecessor_statements', recording_association_storage),
                    embedding_storage.NAME: ('embedding_predecessor_statements', embedding_storage),
                    musical_source_queue_storage.NAME: ('musical_source_predecessor_statements', musical_source_queue_storage),
                    recording_profile_storage.NAME: ('recording_profile_predecessor_statements', recording_profile_storage),
                    recording_redirect_storage.NAME: ('recording_redirect_predecessor_statements', recording_redirect_storage),
                    taste_axis_storage.NAME: ('taste_axis_predecessor_statements', taste_axis_storage),
                    group_input_retention_storage.NAME: ('group_input_retention_predecessor_statements',
                                                         group_input_retention_storage),
                    musical_source_profile_storage.NAME: ('musical_source_profile_predecessor_statements',
                                                          musical_source_profile_storage),
                    radio_launch_storage.NAME: ('radio_launch_predecessor_statements', radio_launch_storage),
                    radio_policy_storage.NAME: ('radio_policy_predecessor_statements', radio_policy_storage),
                    worker_checkpoints.NAME: ('worker_checkpoint_predecessor_statements', worker_checkpoints),
                    paid_access_owner.NAME: ('paid_access_predecessor_statements', paid_access_owner),
                    paid_description_storage.NAME: ('paid_description_predecessor_statements', paid_description_storage),
                    mix_worker_storage.NAME: ('mix_worker_predecessor_statements', mix_worker_storage),
                    work_sequences.NAME: ('work_sequences_predecessor_statements', work_sequences),
                    held_recommendation_state.NAME: ('held_recommendation_predecessor_statements',
                                                     held_recommendation_state),
                    maintenance_clocks.NAME: ('maintenance_clock_predecessor_statements', maintenance_clocks),
                    vibe_worker_storage.NAME: ('vibe_worker_predecessor_statements', vibe_worker_storage),
                    vibe_model_receipts.NAME: ('vibe_model_receipt_predecessor_statements', vibe_model_receipts),
                    playlist_bookkeeping.NAME: ('playlist_bookkeeping_predecessor_statements', playlist_bookkeeping),
                    history_worker_storage.NAME: ('history_worker_predecessor_statements', history_worker_storage),
                    tv_draw_storage.NAME: ('tv_draw_predecessor_statements', tv_draw_storage),
                    policy_worker_storage.NAME: ('policy_worker_predecessor_statements', policy_worker_storage),
                    startup_worker_storage.NAME: ('startup_worker_predecessor_statements', startup_worker_storage),
                    import_and_fit_receipts.NAME: ('import_and_fit_predecessor_statements', import_and_fit_receipts),
                    sync_recovery_storage.NAME: ('sync_recovery_predecessor_statements', sync_recovery_storage),
                    album_playback_storage.NAME: ('album_playback_predecessor_statements', album_playback_storage),
                    tv_storage.NAME: ('tv_storage_predecessor_statements', tv_storage),
                    spotify_storage.NAME: ('spotify_storage_predecessor_statements', spotify_storage),
                    account_binding_receipt.NAME: ('account_binding_receipt_predecessor_statements',
                                                   account_binding_receipt),
                    group_preview_evidence_storage.NAME: ('group_preview_evidence_predecessor_statements',
                                                          group_preview_evidence_storage),
                }
                shipped = destination_schema.definition()['regular_history']
                planned = [[change.version, change.name] for change in regular.CHANGES]
                shared = min(len(shipped), len(planned))
                if shipped[:shared] != planned[:shared]:
                    raise RuntimeError('The shipped schema and compiler histories diverge')
                validate_installed = regular.validate_version

                def validate_shipped_layout(db, version, **options):
                    # The isolated build may have just made the new destination.
                    # Its layout cannot yet be compared to the artifact it creates.
                    if db.execute('SELECT max(version) FROM schema_changes').fetchone()[0] <= len(shipped):
                        validate_installed(db, version, **options)

                with patch.object(regular, 'validate_version', validate_shipped_layout):
                    predecessors = {}
                    for index, change in enumerate(regular.CHANGES):
                        if change.name in compact_owners:
                            key, owner = compact_owners[change.name]
                            regular.migrate(conn, changes=regular.CHANGES[:index], clock=lambda: 0.)
                            predecessors[key] = owner.predecessor_objects(conn)
                    regular.migrate(conn, clock=lambda: 0.)
                # The compiler must never serialize pending work in external
                # artifacts. Empty initialization owns SQL seed state only.
                files = sorted(str(path.relative_to(workspace))
                               for path in Path(workspace).rglob('*') if path.is_file())
                if files:
                    raise RuntimeError('Empty schema construction created external files: '+repr(files))
                return {
                    'format': FORMAT,
                    'numbered_history': [[m.version, m.name] for m in MIGRATIONS[:schema.VERSION]],
                    'regular_history': [[c.version, c.name] for c in regular.CHANGES],
                    'installed_statements': installed_statements,
                    'album_predecessor_statements': album_predecessor_statements,
                    'play_event_predecessor_statements': play_event_predecessor_statements,
                    'artist_predecessor_statements': artist_predecessor_statements,
                    'account_binding_predecessor_statements': account_binding_predecessor_statements,
                    'playlist_item_predecessor_statements': playlist_item_predecessor_statements,
                    'prediction_predecessor_statements': prediction_predecessor_statements,
                    'destination_statements': _objects(conn),
                    'destination_seeds': _seeds(conn),
                    **predecessors,
                }
    finally:
        if previous_home is None:
            os.environ.pop('YT_PLAYLIST_HOME', None)
        else:
            os.environ['YT_PLAYLIST_HOME'] = previous_home


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=OUTPUT,
                        help='directory for the generated SQL and destination JSON resources')
    args = parser.parse_args()
    document = compile_definition()
    from yt_playlist.core.migrations import schema_definition as schema
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for path, content in render(schema).items():
        (args.output_dir / path.name).write_text(content, encoding='utf-8')
    data = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode()
    output = args.output_dir / 'current.json'
    output.write_bytes(data)
    print(json.dumps({'path': str(output), 'sha256': hashlib.sha256(data).hexdigest(),
                      'bytes': len(data), 'statements': len(document['destination_statements']),
                      'seed_statements': len(document['destination_seeds']),
                      'numbered_version': document['numbered_history'][-1][0],
                      'regular_version': document['regular_history'][-1][0]}, sort_keys=True))


if __name__ == '__main__':
    main()
