"""Keep every real credit capture while sharing repeated ordered content."""
import json
from pathlib import Path
import time

from tests.schema.test_populated_groups import populated, storage
from yt_playlist.core import artist_credits, artists
from yt_playlist.core.migrations import Migration, migrate, registry
from yt_playlist.core.migrations import v074_artist_credits as credits_schema


def test_populated_credit_history_and_repeated_ingestion(populated):
    source, conn, output = populated
    before = storage(conn)
    started = time.monotonic()
    migrate(conn, registry=(*registry.MIGRATIONS[:73],
                           Migration(74, 'artist_credit_content', credits_schema.apply)))
    elapsed = time.monotonic() - started
    credits_schema.validate(conn)
    conn.execute('ATTACH DATABASE ? AS original',
                 (Path(source.execute('PRAGMA database_list').fetchone()[2]).as_uri() + '?mode=ro&immutable=1',))
    old = 'SELECT id,entry_id,source,observed_at FROM original.artist_credit_observations'
    new = ('SELECT id,entry_id,source,first_seen FROM artist_credit_observations '
           "WHERE capture_kind='legacy_capture'")
    assert conn.execute(f'SELECT * FROM ({old} EXCEPT {new}) LIMIT 1').fetchone() is None
    assert conn.execute(f'SELECT * FROM ({new} EXCEPT {old}) LIMIT 1').fetchone() is None
    assert conn.execute('SELECT 1 FROM artist_credit_observations '
                        "WHERE capture_kind='legacy_capture' AND (first_seen IS NOT last_seen OR "
                        'processing_count<>1 OR delivery_key IS NOT NULL) LIMIT 1').fetchone() is None
    assert conn.execute('SELECT * FROM original.track_artists EXCEPT SELECT * FROM track_artists LIMIT 1').fetchone() is None
    assert conn.execute('SELECT * FROM track_artists EXCEPT SELECT * FROM original.track_artists LIMIT 1').fetchone() is None
    assert conn.execute('SELECT 1 FROM entry_artist_selections s JOIN artist_credit_observations o '
        "ON o.id=s.observation_id WHERE o.capture_kind<>'selection_snapshot' OR "
        'o.first_seen IS NOT NULL OR o.last_seen IS NOT NULL LIMIT 1').fetchone() is None
    # Check every distinct original document against the actual normalized
    # ordered output, including raw provider IDs, roles and field presence.
    distinct = 0
    for row in conn.execute('SELECT DISTINCT old.credit,old.credits_json,new.credit_id '
            'FROM original.artist_credit_observations old JOIN artist_credit_observations new ON new.id=old.id'):
        actual = artist_credits.read(conn, row['credit_id'])
        expected = json.loads(row['credits_json'])
        assert actual.display_credit == row['credit']
        assert len(actual.members) == len(expected)
        for member, raw in zip(actual.members, expected):
            assert member.credit_text == raw['name']
            assert member.raw_id == raw.get('id')
            assert member.raw_browse_id == raw.get('browseId')
            assert member.role == raw.get('role')
            assert member.presence == sum(bit for key, bit in (('id', 1), ('browseId', 2), ('role', 4)) if key in raw)
        distinct += 1
    samples = conn.execute('SELECT entry_id,credit,credits_json,source FROM original.artist_credit_observations '
        'WHERE id IN (SELECT max(id) FROM original.artist_credit_observations GROUP BY entry_id) '
        'ORDER BY entry_id LIMIT 128').fetchall()
    assert samples
    counts = []
    for repetition in range(3):
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            for row in samples:
                artists.observe(conn, row['entry_id'], row['credit'], json.loads(row['credits_json']),
                                source=row['source'], now=1793260000 + repetition)
        counts.append({name: conn.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                       for name in ('artist_credits', 'artist_credit_members', 'artist_credit_observations')})
    assert counts[0] == counts[1] == counts[2]
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    after = storage(conn)
    names = lambda values: {name: size for name, size in values.items()
                           if 'credit' in name or 'entry_artist_selection' in name}
    report = {'migration_seconds': elapsed, 'legacy_captures': source.execute(
        'SELECT count(*) FROM artist_credit_observations').fetchone()[0],
        'distinct_documents_checked': distinct, 'sample_entries': len(samples),
        'repeated_ingestion_counts': counts, 'before_credit_bytes': names(before),
        'after_credit_bytes': names(after)}
    (output / 'artist-credits.json').write_text(json.dumps(report, indent=2) + '\n')
