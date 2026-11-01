"""Report durability and serving admission use their actual owner boundaries."""
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from yt_playlist.core import recommendation_outputs, recommendation_reports as owner
from yt_playlist.core.migrations import migrate, registry, v093_choice_learning
from yt_playlist.core.store import Store


@pytest.fixture
def predecessor(tmp_path):
    store = Store(tmp_path / 'reports.db')
    store.conn.execute('PRAGMA journal_mode=WAL')
    migrate(store.conn, registry=registry.MIGRATIONS[:92])
    store._bind_repositories()
    yield store
    store.conn.close()


def upgrade(store):
    migrate(store.conn, registry=registry.MIGRATIONS[:93])
    store.init_schema(defer_metadata=True)


def report():
    return {'schema': 1, 'at': 12, 'watermark': 'old-evidence',
            'home_format': {'scores': {'mixes': .5, 'albums': 2.5},
                            'episodes': {'mixes': 1, 'albums': 3}, 'days': {'albums': 3},
                            'vibe_episodes': 2, 'half_life_days': 30},
            'refresh': {'generation': 'events', 'inputs': 'parameters', 'next_at': 912}}


def test_registered_cutover_keeps_exact_bytes_clocks_and_typed_serving_inputs(predecessor):
    raw = json.dumps(report(), indent=2)
    with predecessor.conn:
        predecessor.conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)', ('listening_choices', raw, None))
    upgrade(predecessor)
    assert owner.encoded(predecessor.conn, 'listening_choices') == raw
    assert owner.choice_usage(predecessor.conn) == report()['home_format']
    assert owner.checkpoint(predecessor.conn, 'listening_choices') == {
        'at': 12, 'schema': 1, 'watermark': 'old-evidence', 'report_id': None,
        'generation': 'events', 'inputs': 'parameters', 'next_at': 912}
    assert predecessor.conn.execute('SELECT built_at FROM recommendation_report_publications').fetchone()[0] is None
    assert not predecessor.conn.execute("SELECT 1 FROM rec_proposals WHERE surface='listening_choices'").fetchone()


def test_interrupted_migration_restores_both_findings_owners(predecessor):
    predecessor.rec_proposals.put_proposals('listening_choices', report(), 12)
    predecessor.rec_proposals.put_proposals('choice_interests', {'contexts': []}, 12)
    before = list(predecessor.conn.iterdump())
    with pytest.raises(InterruptedError), predecessor.conn.atomic():
        v093_choice_learning.apply(predecessor.conn, None)
        raise InterruptedError('After all files and numerical relations, before commit')
    assert list(predecessor.conn.iterdump()) == before


def test_lost_report_files_do_not_erase_typed_format_usage_or_replace_last_good(predecessor, monkeypatch):
    from yt_playlist.rec import home_preferences
    upgrade(predecessor)
    predecessor.rec_proposals.put_proposals('listening_choices', report(), 12)
    with monkeypatch.context() as patch:
        patch.setattr(owner, 'encoded', lambda *args: pytest.fail('Serving format decoded diagnostics'))
        assert home_preferences.default_format(predecessor) == 'albums'
    paths = [Path(row[0]) for row in predecessor.conn.execute('SELECT filename FROM recommendation_report_files')]
    for path in paths:
        path.unlink()
    assert predecessor.rec_proposals.get_proposals('listening_choices') is None
    assert home_preferences.default_format(predecessor) == 'albums'
    with pytest.raises(ValueError):
        predecessor.rec_proposals.put_proposals('listening_choices', {'at': float('nan')}, 13)
    assert owner.checkpoint(predecessor.conn, 'listening_choices')['watermark'] == 'old-evidence'


def test_backup_restart_and_rollback_retain_owned_report_files(predecessor, tmp_path):
    upgrade(predecessor)
    value = {**report(), 'detail': 'é' * 180000}
    predecessor.rec_proposals.put_proposals('listening_choices', value, 12)
    raw = owner.encoded(predecessor.conn, 'listening_choices')
    assert len(raw.encode()) > 256 * 1024
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with pytest.raises(InterruptedError), predecessor.conn.atomic():
        owner.publish(predecessor.conn, owner.prepare('listening_choices', json.dumps(report()), 13))
        raise InterruptedError('Before head commit')
    assert hashlib.sha256(owner.encoded(predecessor.conn, 'listening_choices').encode()).hexdigest() == digest
    with predecessor.conn.atomic():
        backup = recommendation_outputs.export(predecessor.conn, tmp_path / 'report-backup')
    recommendation_outputs.restore(predecessor.conn, backup)
    reopened = Store(predecessor._db_path)
    try:
        reopened.init_schema()
        assert reopened.rec_proposals.get_proposals('listening_choices') == value
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        reopened.conn.close()


def test_sql_rejects_wrong_usage_owner_and_mutation_after_publication(predecessor):
    upgrade(predecessor)
    predecessor.rec_proposals.put_proposals('listening_choices', report(), 12)
    predecessor.rec_proposals.put_proposals('model_health', {'version': 1, 'inputs': 'model'}, 12)
    identity = predecessor.conn.execute("SELECT publication_id FROM recommendation_report_heads WHERE surface='model_health'").fetchone()[0]
    for sql, args in [
        ('INSERT INTO choice_format_usage VALUES (?,?,?,?)', (identity, 'radio', 'days', 1)),
        ('UPDATE choice_format_usage SET value=99', ()),
        ('DELETE FROM choice_format_usage', ()),
        ('UPDATE recommendation_report_publications SET inputs_key=?', ('changed',)),
        ('UPDATE recommendation_report_publications SET status=?', ('building',)),
        ("INSERT INTO rec_proposals VALUES ('model_health','{}',12)", ()),
    ]:
        with pytest.raises(sqlite3.IntegrityError), predecessor.conn.atomic():
            predecessor.conn.execute(sql, args)


def test_refresh_keeps_completed_output_and_usage_while_clearing_model_findings(predecessor):
    from yt_playlist.rec.invalidation import artist_reconciled, clear_model_display
    upgrade(predecessor)
    model_surfaces = ('home_prepared', 'home_tuned', 'home_choice', 'album_prepared')
    for surface in (*model_surfaces, 'home_discovery'):
        predecessor.rec_proposals.put_proposals(surface, {'at': 12}, 12)
    predecessor.rec_proposals.put_proposals('listening_choices', report(), 12)
    predecessor.rec_proposals.put_proposals('choice_interests', {'contexts': []}, 12)
    with pytest.raises(InterruptedError), predecessor.conn.atomic():
        clear_model_display(predecessor.conn)
        raise InterruptedError('Before model replacement commit')
    assert all(predecessor.rec_proposals.get_proposals(surface) == {'at': 12} for surface in model_surfaces)
    with predecessor.conn.atomic():
        clear_model_display(predecessor.conn)
    assert predecessor.rec_proposals.get_proposals('home_choice') is None
    assert all(predecessor.rec_proposals.get_proposals(surface) == {'at': 12}
               for surface in model_surfaces if surface != 'home_choice')
    assert predecessor.rec_proposals.get_proposals('home_discovery') == {'at': 12}
    assert predecessor.rec_proposals.get_proposals('listening_choices') == report()
    with predecessor.conn.atomic():
        artist_reconciled(predecessor.conn)
    assert predecessor.rec_proposals.get_proposals('home_discovery') == {'at': 12}
    assert predecessor.rec_proposals.get_proposals('choice_interests') is None
    assert predecessor.rec_proposals.get_proposals('listening_choices') == report()
    assert not predecessor.conn.execute('SELECT 1 FROM choice_learning_publications').fetchone()
    assert {row[0] for row in predecessor.conn.execute('SELECT surface FROM recommendation_report_heads')} == {'listening_choices'}
