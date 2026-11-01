"""Cut over collection recovery, account feedback and owner-specific checkpoints."""
from pathlib import Path
import re
import sqlite3

from yt_playlist.core import (account_auth_state, album_feedback_storage, album_playback_context, album_tuning_state, content_preparation_state,
    discovery_checkpoints, group_preparation_state,
    model_selection_heads, model_state, playback_bindings, radio_provenance, radio_storage,
    ranking_checkpoints, recommendation_outputs, road_trip_draft_storage, sync_state)
from .runner import MigrationError

DEFINITIONS = ('schema_collection_state.sql','schema_session_state.sql','schema_sync_state.sql','schema_model_state.sql')
KEYS = frozenset({'album_feedback','album_menu_tuning','auth_expired_identities','last_sync_at',
    'last_sync_attempt_at','last_sync_outcome','library_sync_state','listening_album_pending',
    'listening_playback_bindings','radio_current_session','radio_playlist_a_ytm','radio_playlist_b_ytm',
    'radio_playlist_ytm','radio_session_playlist_ids','rec_artist_content_model','rec_artist_fit_inputs',
    'rec_candidate_preferences','rec_candidates_expire_at','rec_content_generation','rec_content_genre_revision',
    'rec_content_inputs','rec_content_model','rec_fit_inputs','rec_group_shadow_checkpoint',
    'rec_group_shadow_progress','rec_mode_fit_inputs','rec_preparation_basis','rec_preparation_fit',
    'rec_preparation_full','rec_selection_active_run','rec_selection_display_run','rec_selection_live_run',
    'rec_selection_restore','rec_vibe_version'})
PREFIXES = ('enrich_provider_refresh:','discovery:artist-image:',discovery_checkpoints.PREFIX)


def transform(sql):
    return '' if re.match(r'CREATE TABLE\s+"?road_trip_drafts"?\s*\(',sql.lstrip(),re.I) else sql


def statements():
    for definition in DEFINITIONS:
        pending = ''
        for line in Path(__file__).with_name(definition).read_text().splitlines(True):
            pending += line
            if sqlite3.complete_statement(pending):
                yield pending
                pending = ''
        if pending.strip():
            raise MigrationError('Incomplete collection schema: '+definition)
    yield from settings_fences()


def settings_fences():
    keys = ','.join("'"+key.replace("'","''")+"'" for key in sorted(KEYS))
    prefixes = ' OR '.join(f"substr(NEW.key,1,{len(prefix)})='{prefix}'" for prefix in PREFIXES)
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER collection_settings_{event.lower()} BEFORE {event} ON settings '
               f'WHEN NEW.key IN ({keys}) OR {prefixes} '
               "BEGIN SELECT RAISE(ABORT,'Collection state requires its domain owner'); END;")


def apply(conn,context):
    original = {key:value for key,value in conn.execute('SELECT key,value FROM settings')
                if key in KEYS or key.startswith(PREFIXES)}
    drafts = list(conn.execute('SELECT recipe_id,state,updated_at FROM road_trip_drafts'))
    try:
        # Decode all supported JSON before replacing an owner. A failed
        # conversion rolls back schema, references and migration history.
        decoded = {key:recommendation_outputs.decode_json(value) for key,value in original.items()
                   if key not in model_state.FIELDS and key not in sync_state.STAMPS
                   and key not in radio_provenance.KEYS and key!='rec_mode_fit_inputs'
                   and key not in model_selection_heads.KEYS and key not in ranking_checkpoints.KEYS
                   and not key.startswith(discovery_checkpoints.PREFIX)}
        decoded_drafts = [(recipe,recommendation_outputs.decode_json(raw),at) for recipe,raw,at in drafts]
        conn.execute('DROP TABLE road_trip_drafts')
        for sql in statements():
            conn.execute(sql)
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
        for recipe,state,at in decoded_drafts:
            if not isinstance(state,dict):
                raise ValueError('Legacy draft must be an object')
            road_trip_draft_storage.save(conn,recipe,state,at,legacy=True)
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
                value = original[key] or None
                sync_state.set_stamp(conn,key,value)
        model_state.put(conn,{key:value for key,value in original.items() if key in model_state.FIELDS})
        group_preparation_state.put(conn,{key:value for key,value in original.items()
                                         if key in group_preparation_state.KEYS},legacy=True)
        content_preparation_state.put(conn,{key:value for key,value in original.items()
                                           if key in content_preparation_state.KEYS})
        model_selection_heads.put(conn,{key:value for key,value in original.items() if key in model_selection_heads.KEYS})
        ranking_checkpoints.put(conn,{key:value for key,value in original.items() if key in ranking_checkpoints.KEYS})
        album_tuning_state.put(conn,{key:value for key,value in original.items() if key in album_tuning_state.KEYS})
        conn.executemany('DELETE FROM settings WHERE key=?',[(key,) for key in original])
    except (ValueError,TypeError,KeyError,sqlite3.IntegrityError) as exc:
        raise MigrationError(f'Collection state conversion failed; original retained: {exc}') from exc
