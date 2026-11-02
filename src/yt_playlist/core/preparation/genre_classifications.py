"""Prepare selected genre references while retaining installed labels and writers."""
import hashlib
import json

from .. import genre_contract as genre_classifications, genre_identity
from .rows import Job, main as run


MAX_LABEL_BYTES = 4096


def describe(conn, owner, key, row):
    label = row['genre']
    if isinstance(label, bytes):
        label = label.decode('utf-8')
    if label is not None and (not isinstance(label, str) or len(label.encode()) > MAX_LABEL_BYTES):
        raise ValueError('A selected genre exceeds its preparation label byte budget')
    genre, reason = genre_identity.resolve(conn, label)
    disposition = ('null' if label is None else 'blank' if label == '' else
                   'resolved' if genre is not None else reason)
    snapshot = json.dumps({'label': label, 'genre_key': genre, 'disposition': disposition},
                          ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(snapshot.encode('utf-8')).hexdigest()
    return (owner, key, label, genre, disposition, digest), snapshot


def copy(conn, owner, key, row):
    values, snapshot = describe(conn, owner, key, row)
    _, _, label, genre, disposition, digest = values
    conn.execute('INSERT INTO prepared_genre_originals VALUES (?,?,?,?) '
                 'ON CONFLICT(owner,source_key,digest) DO NOTHING', (owner, key, digest, snapshot))
    conn.execute('INSERT INTO prepared_genre_classifications VALUES (?,?,?,?,?,?) '
                 'ON CONFLICT(owner,source_key) DO UPDATE SET raw_label=excluded.raw_label,'
                 'genre_key=excluded.genre_key,disposition=excluded.disposition,source_digest=excluded.source_digest',
                 (owner, key, label, genre, disposition, digest))


def delete(conn, owner, key):
    conn.execute('DELETE FROM prepared_genre_classifications WHERE owner=? AND source_key=?', (owner, key))


JOB = Job('genre_classifications', 79, 85,
    {owner: (column,) for owner, column in genre_classifications.OWNERS.items()}, (
        ('prepared_genre_originals', '''CREATE TABLE prepared_genre_originals (
            owner TEXT NOT NULL CHECK(owner IN ('recordings','albums','discovered_artists')),
            source_key TEXT NOT NULL CHECK(json_valid(source_key)),
            digest TEXT NOT NULL CHECK(length(digest)=64),
            source_snapshot TEXT NOT NULL CHECK(json_valid(source_snapshot)),
            PRIMARY KEY(owner,source_key,digest)) WITHOUT ROWID'''),
        ('prepared_genre_classifications', '''CREATE TABLE prepared_genre_classifications (
            owner TEXT NOT NULL, source_key TEXT NOT NULL,
            raw_label TEXT, genre_key TEXT,
            disposition TEXT NOT NULL CHECK(disposition IN ('null','blank','resolved','unknown_genre','ambiguous_genre')),
            source_digest TEXT NOT NULL,
            PRIMARY KEY(owner,source_key),
            FOREIGN KEY(owner,source_key,source_digest) REFERENCES prepared_genre_originals(owner,source_key,digest),
            CHECK((genre_key IS NOT NULL)=(disposition='resolved')),
            CHECK((raw_label IS NULL)=(disposition='null')),
            CHECK(disposition<>'blank' OR raw_label='')) WITHOUT ROWID'''),
    ), copy, delete, dependencies=('genre_catalog', 'genre_alias'),
    projections={owner: (column, 'CASE WHEN genre IS NULL THEN NULL ELSE '
                         f'COALESCE(substr(CAST(genre AS BLOB),1,{MAX_LABEL_BYTES+1}),X\'\') END AS genre')
                 for owner, column in genre_classifications.OWNERS.items()})


def main(argv=None):
    return run(JOB, argv)


if __name__ == '__main__':
    raise SystemExit(main())
