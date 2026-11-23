"""Bounded SQL facts and immutable identities for shared vibe models."""
import json
from itertools import batched

from yt_playlist.core.activity import check_stopping
from yt_playlist.core import vibe_versions
from yt_playlist.core.audio_vibe_contract import fit_family_prefix


def album_facts(conn, recordings):
    """Album identity and distinct-owner count in bounded indexed batches."""
    identities, result = list(recordings), {}
    for start in range(0, len(identities), 128):
        check_stopping()
        batch = identities[start:start+128]
        marks = ','.join('?' for _ in batch)
        sql = ('SELECT o.recording_id,t.album_id FROM album_track_occurrences o '
            'JOIN album_tracklists t ON t.id=o.tracklist_id JOIN albums a ON a.current_tracklist_id=t.id '
            'WHERE o.recording_id IN (' + marks + ') '
            'UNION ALL SELECT recording_id,album_id FROM album_item_observations WHERE recording_id IN (' + marks + ') '
            'UNION ALL SELECT e.recording_id,i.album_id FROM track_sources e '
            'JOIN album_item_observations i ON i.entry_id=e.id WHERE e.recording_id IN (' + marks + ')')
        result.update((row[0], (row[1], row[2])) for row in conn.execute(
            'SELECT recording_id,min(album_id),count(DISTINCT album_id) FROM (' + sql + ') GROUP BY recording_id', batch * 3))
    return result


def training_rows(conn, vibe, limit, *, pattern=None):
    """Return at most limit plus one original rows so callers detect overflow."""
    measurement = ' AND version.value GLOB ?' if pattern is not None else ''
    versions = (pattern, pattern) if pattern is not None else (fit_family_prefix() + '*',)
    return [dict(row) for row in conn.execute('SELECT v.recording_id,v.measurement_id,v.artist_key,v.subgenre_key,'
        'm.score,m.support,m.source,m.input_digest,version.value AS exact_version '
        'FROM vibe_recording_fits v JOIN measurements m ON m.id=v.measurement_id '
        'JOIN vibe_versions version ON version.id=m.model_version_id '
        "WHERE v.vibe=? AND m.source='audio_measurement' AND v.source='audio_measurement' "
        'AND m.support>0 AND v.inputs_version GLOB ?' + measurement +
        ' ORDER BY v.recording_id LIMIT ?', (vibe, *versions, limit + 1))]


def linked(conn, prediction):
    row = conn.execute('SELECT model_generation FROM predictions WHERE id=?',
                       (prediction,)).fetchone()
    return row[0].hex() if row and row[0] is not None else None


def subject(conn, prediction):
    return conn.execute('SELECT vibe,recording_id FROM predictions WHERE id=?', (prediction,)).fetchone()


def receipt_marker(conn, prediction):
    row = conn.execute('SELECT provenance_fingerprint,provenance_state,model_generation FROM predictions '
                       'WHERE id=? AND provenance_state IS NOT NULL', (prediction,)).fetchone()
    return (row[0], row[1], row[2].hex() if row[2] is not None else None) if row else None


def insert_measurements(conn, rows):
    versions = {value: vibe_versions.intern(conn, value) for value in dict.fromkeys(row[6] for row in rows)}
    conn.executemany('INSERT INTO measurements '
                     '(id,recording_id,vibe,score,support,source,model_version_id,input_digest,measured_at) '
                     'VALUES (?,?,?,?,?,?,?,?,?)',
                     ((*row[:6], versions[row[6]], *row[7:]) for row in rows))


def retain_measurements(conn, identities):
    for batch in batched(dict.fromkeys(identities), 128):
        slots = ','.join('?' for _ in batch)
        found = conn.execute(f'SELECT count(*) FROM measurements WHERE id IN ({slots})', batch).fetchone()[0]
        if found != len(batch):
            raise ValueError('Model evidence requires existing original measurements')
        conn.execute(f'UPDATE measurements SET used_by_model=1 WHERE used_by_model=0 AND id IN ({slots})', batch)


def referenced(conn, generation, *, inline_heads=True):
    from .vibe_model_files import HEAD_PREFIX

    head = (conn.execute('SELECT 1 FROM vibe_definitions WHERE joint_head=? LIMIT 1', (generation,)).fetchone()
            if inline_heads else conn.execute('SELECT 1 FROM settings WHERE key GLOB ? AND value=? LIMIT 1',
                                               (HEAD_PREFIX + '*', generation)).fetchone())
    return (head
            or conn.execute("SELECT 1 FROM predictions WHERE model_generation=? AND provenance_state='ready' LIMIT 1",
                            (bytes.fromhex(generation),)).fetchone())


def joint_state(conn, vibe):
    row = conn.execute('SELECT joint_head,joint_outcome FROM vibe_definitions WHERE vibe=?', (vibe,)).fetchone()
    return (row[0], json.loads(row[1] or '{}')) if row else (None, {})


def measurement_rows(conn, identities):
    if not 0 < len(identities) <= 128:
        raise ValueError('Shared model measurements require a bounded batch')
    return [dict(row) for row in conn.execute(
        'SELECT m.id,m.recording_id,m.vibe,m.score,m.support,m.source,m.input_digest,'
        'version.value AS exact_version FROM measurements m JOIN vibe_versions version ON version.id=m.model_version_id '
        'WHERE m.id IN (' + ','.join('?' for _ in identities) + ')', identities)]


class VibeModelQueries:
    """Connection-only queries shared by runtime repositories and offline rebuilds."""

    def __init__(self, conn):
        self.conn = conn

    def assert_idle(self):
        if self.conn.in_transaction:
            raise RuntimeError('Shared model collection requires committed SQL')

    def file_reference(self, generation):
        from yt_playlist.repos import vibe_model_file_writes
        return vibe_model_file_writes.selected(self.conn, generation)

    def file_digest(self, identity):
        from yt_playlist.repos import vibe_model_file_writes
        return vibe_model_file_writes.digest(self.conn, identity)

    def measurements(self, identities):
        return measurement_rows(self.conn, identities)

    def receipt_facts(self, prediction, artists):
        row = self.conn.execute('SELECT vibe,recording_id,model_generation,provenance_fingerprint,provenance_state '
                                'FROM predictions WHERE id=?', (prediction,)).fetchone()
        generation = row[2].hex() if row and row[2] is not None else None
        return {'prediction': tuple(row[:2]) if row else None, 'generation': generation,
                'marker': (row[3], row[4], generation) if row and row[4] is not None else None,
                'artists': {artist for artist in artists if type(artist) is int and self.conn.execute(
                    'SELECT 1 FROM artists WHERE id=?', (artist,)).fetchone()}}

    def install_receipts(self, generations, roots, markers):
        # Explicit rebuild callers already own their SQL batch. Runtime model
        # publication uses the prediction repository's ordered publication.
        if not self.conn.in_transaction:
            raise RuntimeError('Receipt rebuild requires its SQL batch')
        retain_measurements(self.conn, roots)
        models = dict(generations)
        self.conn.executemany("UPDATE predictions SET model_generation=?,provenance_fingerprint=?,"
                              "provenance_bytes=?,provenance_state='ready' WHERE id=? AND provenance_state IS NULL",
                              ((models[pid], fingerprint, size, pid) for pid, fingerprint, size in markers))

    def referenced(self, generation, *, inline_heads=True):
        if type(generation) is int:
            digest = self.file_digest(generation)
            if digest is None:
                return False
            return self.file_reference(digest) == generation and bool(
                referenced(self.conn, digest, inline_heads=inline_heads))
        if self.file_reference(generation) is not None:
            return False
        return bool(referenced(self.conn, generation, inline_heads=inline_heads))
