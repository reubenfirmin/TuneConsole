"""Opt-in populated upgrades always work on a disposable database and file bundle."""
import hashlib
import json
from pathlib import Path
import sqlite3
import shutil
import time

import pytest

from tests import file_custody_support as custody
from yt_playlist.core import group_documents
from yt_playlist.core.migrations import Migration, migrate, registry
from yt_playlist.core.migrations import v070_policy_documents as policies
from yt_playlist.core.migrations import v071_album_descriptions as descriptions
from yt_playlist.core.migrations import v072_compact_groups as groups


def fingerprint(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def storage(conn):
    return {name: size for name, size in conn.execute(
        'SELECT name,sum(pgsize) FROM dbstat GROUP BY name')}


@pytest.fixture
def populated(request, tmp_path):
    option = request.config.getoption('--schema-source-db')
    if not option:
        pytest.skip('Supply --schema-source-db with an offline predecessor snapshot')
    path = Path(option).resolve(strict=True)
    assert not Path(str(path) + '-wal').exists(), 'Use an offline, checkpointed snapshot'
    original = fingerprint(path)
    source = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    source.row_factory = sqlite3.Row
    source.execute('PRAGMA query_only=ON')
    source.execute('BEGIN')
    assert source.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 69
    target = sqlite3.connect(tmp_path / 'populated.db', uri=True)
    target.row_factory = sqlite3.Row
    upgraded = request.config.getoption('--schema-upgraded-db')
    donor = sqlite3.connect(Path(upgraded).resolve().as_uri() + '?mode=ro&immutable=1', uri=True) if upgraded else source
    try:
        if upgraded:
            donor.execute('PRAGMA query_only=ON')
            donor.execute('BEGIN')
        donor.backup(target)
        files = custody.export_files(donor, tmp_path / 'populated.db')
    finally:
        if upgraded:
            donor.close()
    with target:
        custody.restore_files(target, files)
    evidence = request.config.getoption('--schema-evidence-dir')
    if evidence:
        shutil.copytree(Path(evidence).resolve(strict=True), tmp_path / 'populated.db.evidence')
    target.execute('PRAGMA foreign_keys=ON')
    try:
        yield source, target, tmp_path
    finally:
        target.close()
        source.close()
        assert fingerprint(path) == original, 'The source snapshot changed'


def test_complete_group_history_survives_compaction(populated, request):
    source, conn, output = populated
    started = time.monotonic()
    before = storage(source)
    counts = {table: source.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
              for table in groups.OLD_TABLES}
    head = source.execute('SELECT generation FROM rec_group_head').fetchone()[0]
    migrations = (*registry.MIGRATIONS[:69],
        Migration(70, 'policy_documents', policies.apply),
        Migration(71, 'album_description_owners', descriptions.apply),
        Migration(72, 'compact_groups', groups.apply, rebuild_tables=True))
    migrate(conn, registry=migrations)
    policies.validate(conn)
    descriptions.validate(conn)
    groups.validate(conn)
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    assert conn.execute('SELECT count(*) FROM group_generations').fetchone()[0] == counts['rec_group_generations']
    assert conn.execute('SELECT count(*) FROM generation_groups').fetchone()[0] == counts['rec_group_definitions']
    assert conn.execute('SELECT g.external_key FROM group_head h JOIN group_generations g '
                        'ON g.id=h.generation_id').fetchone()[0] == head
    conn.execute('ATTACH DATABASE ? AS original',
                 (Path(source.execute('PRAGMA database_list').fetchone()[2]).as_uri() + '?mode=ro&immutable=1',))
    # Compare every original rank-independent membership and score through the
    # preserved alias, including missing exact catalog keys. This also catches
    # accidental association through a display name or a non-unique alias.
    old = 'SELECT version,identity_key,similarity,support FROM original.rec_group_members'
    new = ('SELECT a.external_version,r.identity_key,m.similarity,m.support FROM group_version_aliases a '
           'JOIN group_members m ON m.group_version_id=a.group_version_id JOIN recordings r ON r.id=m.recording_id '
           'UNION ALL SELECT a.external_version,m.raw_key,m.similarity,m.support FROM group_version_aliases a '
           'JOIN unresolved_group_members m ON m.group_version_id=a.group_version_id')
    assert conn.execute(f'SELECT * FROM ({old} EXCEPT SELECT * FROM ({new})) LIMIT 1').fetchone() is None
    assert conn.execute(f'SELECT * FROM ({new}) EXCEPT {old} LIMIT 1').fetchone() is None
    for row in conn.execute('SELECT * FROM group_versions'):
        group_documents.read(row['definition_filename'], row['definition_digest'],
                             row['definition_bytes'], row['definition_offset'], 'definition')
    after = storage(conn)
    report = {'seconds': time.monotonic() - started, 'resumed_from': request.config.getoption('--schema-upgraded-db'),
              'source_counts': counts,
              'group_versions': conn.execute('SELECT count(*) FROM group_versions').fetchone()[0],
              'group_members': conn.execute('SELECT count(*) FROM group_members').fetchone()[0],
              'before_bytes': before, 'after_bytes': after,
              'group_file_bytes': sum(p.stat().st_size for p in group_documents.directory(conn).rglob('*') if p.is_file())}
    (output / 'group-compaction.json').write_text(json.dumps(report, indent=2) + '\n')
