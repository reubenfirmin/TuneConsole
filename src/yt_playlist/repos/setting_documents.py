"""Capture retained setting manifests without opening their files."""
from yt_playlist.core import spotify_job_state, taste_control_state, experiment_decision_state, workflow_settings
from yt_playlist.core.schema_contract import MigrationError


def parts(conn, header, table, *, generation='generation', digest='digest',
          surface=None, prefix='', nullable_part=False, limit=257):
    rows = conn.execute('SELECT position,filename,'+digest+' AS content_digest,byte_size FROM '+table+
        ' WHERE source=? AND '+generation+'=? ORDER BY position LIMIT ?',
        (header['source'], header[generation], limit)).fetchall()
    return {**dict(header), 'storage': 'parts', 'surface': surface or header['source'],
            'prefix': prefix, 'nullable_part': nullable_part, 'parts': [dict(row) for row in rows]}


def _document(conn, owner, revision, default):
    row = conn.execute('SELECT generation FROM '+owner.tables[0]+' WHERE revision=?', (revision,)).fetchone()
    if row is None:
        raise MigrationError('Published document generation is missing')
    if row[0] is None:
        return True, default, None
    header, original, _ = owner.preparation.tables(owner.source)
    captured = conn.execute('SELECT * FROM '+header+' WHERE source=? AND '+owner.generation_column+'=?',
                            (owner.source, row[0])).fetchone()
    if captured is None:
        raise MigrationError('Published document original is missing')
    nullable = owner.generation_column == 'preparation_revision'
    return True, None, parts(conn, captured, original, generation=owner.generation_column,
        digest='content_digest' if nullable else 'digest', prefix='original:' if nullable else '',
        nullable_part=nullable, limit=513 if nullable else 257,
        surface='library_sync' if owner.source == 'library_sync_state' else None)


def prepared(conn, key, default=None):
    if workflow_settings.owns(key):
        from yt_playlist.core.preparation import workflow_publication, workflow_receipts
        owner = workflow_publication.owner_for(key)
        revision = owner.selected(conn)
        if revision is not None:
            header = owner.capture(conn, revision, key)
            return (True, default, None) if header is None else (True, None, parts(
                conn, header, workflow_receipts.table(owner.name, 'parts'),
                generation='preparation_revision', digest='content_digest', prefix='original:',
                nullable_part=True, limit=513))
    owner = None
    if key == spotify_job_state.KEY:
        from yt_playlist.core.preparation.import_publication import OWNER
        owner = OWNER
    elif key in (*taste_control_state.KEYS, *experiment_decision_state.KEYS):
        from yt_playlist.core.preparation.taste_control_publication import SOURCES
        owner = SOURCES[key]
    elif key == 'library_sync_state':
        from yt_playlist.core.preparation.library_sync_publication import RESULT
        owner = RESULT
    elif key in ('listening_playback_bindings', 'listening_album_pending'):
        from yt_playlist.core.preparation.playback_publication import SOURCES
        owner = SOURCES[key]
    elif key in ('album_feedback', 'auth_expired_identities'):
        from yt_playlist.core.preparation.collection_feedback_publication import SOURCES
        owner = SOURCES[key]
    if owner is not None:
        revision = owner.selected(conn)
        if revision is not None:
            return _document(conn, owner, revision, default)
    from yt_playlist.core import radio_reads
    if key in radio_reads.ORIGIN_KEYS:
        revision = radio_reads.origin_selected(conn)
        if revision is not None:
            header = conn.execute('SELECT g.* FROM published_radio_origin_sources l '
                'JOIN prepared_radio_origin_generations g USING(source,generation) '
                'WHERE l.revision=? AND l.source=?', (revision, key)).fetchone()
            return (True, default, None) if header is None else (True, None, parts(
                conn, header, 'prepared_radio_origin_original_parts', surface='radio_origin', prefix=key+':'))
    elif key == 'radio_current_session':
        revision = radio_reads.session_selected(conn)
        if revision is not None:
            header = conn.execute('SELECT * FROM prepared_radio_sessions WHERE generation=?', (revision,)).fetchone()
            if header is None:
                raise MigrationError('Published Radio checkpoint is missing')
            if not header['source_present']:
                return True, default, None
            return True, None, {**dict(header), 'storage': 'plain'} if header['filename'] else None
    return False, None, None
