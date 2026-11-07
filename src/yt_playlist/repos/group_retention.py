"""Guarded SQL installation of a bounded group retirement selection."""
from yt_playlist.core.activity import check_cancellable_work


def apply(conn, selected):
    if not conn.in_transaction:
        raise RuntimeError('Musical group retirement requires guarded publication')
    if not conn.execute('SELECT 1 FROM group_retention_state WHERE id=1 AND roots_complete=1').fetchone():
        raise RuntimeError('Musical group evidence backfill is incomplete')
    if 'revision' in selected and conn.execute(
            'SELECT revision FROM group_retention_epoch WHERE id=1').fetchone()[0] != selected['revision']:
        raise RuntimeError('Musical group retirement roots changed')
    if 'walk' in selected:
        for statement, parameters in selected['walk']:
            check_cancellable_work()
            conn.execute(statement, parameters)
    elif 'edges' in selected:
        for generation_id, source, target in selected['edges']:
            conn.execute('INSERT INTO group_lineage_retirements VALUES (?,?)', (source, target))
            conn.execute('DELETE FROM group_generation_lineage WHERE generation_id=? AND source_alias=? AND target_alias=?',
                         (generation_id, source, target))
            conn.execute('DELETE FROM group_lineage_retirements WHERE source_alias=? AND target_alias=?', (source, target))
            conn.execute('DELETE FROM group_lineage WHERE source_alias=? AND target_alias=? AND NOT EXISTS('
                'SELECT 1 FROM group_generation_lineage WHERE source_alias=? AND target_alias=?)',
                (source, target, source, target))
    else:
        generation_id = selected['generation']
        conn.execute("INSERT INTO group_builds VALUES (?,'cleanup',0,0,NULL,NULL)", (generation_id,))
        conn.execute('DELETE FROM group_publications WHERE generation_id=?', (generation_id,))
