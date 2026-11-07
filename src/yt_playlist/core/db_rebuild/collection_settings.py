"""Import supported predecessor settings through current collection owners."""
import sqlite3

from .. import (account_auth_state, album_feedback_storage, album_playback_context,
    album_tuning_state, content_preparation_state, discovery_checkpoints,
    group_preparation_state, model_selection_heads, model_state, playback_bindings,
    radio_provenance, radio_storage, ranking_checkpoints, recommendation_outputs, sync_state)
from ..schema_contract import MigrationError


KEYS = frozenset({'album_feedback','album_menu_tuning','auth_expired_identities','last_sync_at',
    'last_sync_attempt_at','last_sync_outcome','library_sync_state','listening_album_pending',
    'listening_playback_bindings','radio_current_session','radio_playlist_a_ytm','radio_playlist_b_ytm',
    'radio_playlist_ytm','radio_session_playlist_ids','rec_artist_content_model','rec_artist_fit_inputs',
    'rec_candidate_preferences','rec_candidates_expire_at','rec_content_generation','rec_content_genre_revision',
    'rec_content_inputs','rec_content_model','rec_fit_inputs','rec_group_shadow_checkpoint',
    'rec_group_shadow_progress','rec_mode_fit_inputs','rec_preparation_basis','rec_preparation_fit',
    'rec_preparation_full','rec_selection_active_run','rec_selection_display_run','rec_selection_live_run',
    'rec_selection_restore','rec_vibe_version'})
TASTE_KEYS = frozenset({'taste_settings_checkpoint_v1','taste_comparison_baseline_v2','ppr_experiment_decisions'})
PREFIXES = ('enrich_provider_refresh:','discovery:artist-image:',discovery_checkpoints.PREFIX)


def copy_settings(conn, original, context):
    """The rebuild owns the stable input snapshot, DDL and transaction."""
    try:
        decoded = {key:recommendation_outputs.decode_json(value) for key,value in original.items()
                   if key not in model_state.FIELDS and key not in sync_state.STAMPS
                   and key not in radio_provenance.KEYS and key!='rec_mode_fit_inputs'
                   and key not in model_selection_heads.KEYS and key not in ranking_checkpoints.KEYS
                   and not key.startswith(discovery_checkpoints.PREFIX)}
        if 'album_feedback' in decoded:
            album_feedback_storage.import_legacy(conn,decoded['album_feedback'])
        if account_auth_state.KEY in decoded:
            account_auth_state.import_legacy(conn,decoded[account_auth_state.KEY])
        radio_provenance.import_legacy(conn,original)
        for key,value in original.items():
            if key.startswith(discovery_checkpoints.PREFIX):
                if value!='1':
                    raise ValueError('Unsupported discovery fetch marker')
                discovery_checkpoints.remember(conn,key[len(discovery_checkpoints.PREFIX):])
        for key,value in decoded.items():
            if key.startswith(PREFIXES[0]):
                if (not isinstance(value,dict) or set(value)-{'cursor','cutoff','fingerprint','revision'}
                        or not {'cursor','cutoff','fingerprint'}<=value.keys() or not isinstance(value['fingerprint'],str)):
                    raise ValueError('Unsupported provider refresh frontier')
                conn.execute('INSERT INTO provider_refresh_frontiers VALUES (?,?,?,?,?)',
                    (key[len(PREFIXES[0]):],value['cursor'],value['cutoff'],value['fingerprint'],value.get('revision',1)))
            elif key.startswith(PREFIXES[1]):
                owner = key[len(PREFIXES[1]):]
                if not owner.isdecimal():
                    raise ValueError('Artist image retry has no exact artist owner')
                conn.execute('INSERT INTO discovery_artist_image_state VALUES (?,?)',(int(owner),value))
        if 'listening_playback_bindings' in decoded:
            playback_bindings.import_legacy(conn,original['listening_playback_bindings'])
        if album_playback_context.KEY in decoded:
            album_playback_context.put(conn,decoded[album_playback_context.KEY])
        if 'radio_current_session' in decoded and decoded['radio_current_session'] is not None:
            radio_storage.save(conn,decoded['radio_current_session'],expected=None,legacy=True,now=context.now)
        if 'library_sync_state' in decoded and decoded['library_sync_state']:
            state = decoded['library_sync_state']
            sync_state.write(conn,state,state.get('last_attempt_at'),
                last_success=float(original['last_sync_at']) if original.get('last_sync_at') else None)
        for key in sync_state.STAMPS:
            if key in original:
                sync_state.set_stamp(conn,key,original[key] or None)
        model_state.put(conn,{key:value for key,value in original.items() if key in model_state.FIELDS})
        group_preparation_state.put(conn,{key:value for key,value in original.items()
                                         if key in group_preparation_state.KEYS},legacy=True)
        content_preparation_state.put(conn,{key:value for key,value in original.items()
                                           if key in content_preparation_state.KEYS})
        model_selection_heads.put(conn,{key:value for key,value in original.items() if key in model_selection_heads.KEYS})
        ranking_checkpoints.put(conn,{key:value for key,value in original.items() if key in ranking_checkpoints.KEYS})
        album_tuning_state.put(conn,{key:value for key,value in original.items() if key in album_tuning_state.KEYS})
    except (ValueError,TypeError,KeyError,sqlite3.IntegrityError) as exc:
        raise MigrationError(f'Collection state conversion failed; original retained: {exc}') from exc
