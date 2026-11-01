"""Opt-in learning cutover on the retained, isolated actual library."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

from yt_playlist.core import (choice_learning_state as owner, recommendation_reports, tv_recommendation_state,
                              recommendation_setting_outputs)
from yt_playlist.core.migrations import current, migrate, registry, v093_choice_learning, v094_collection_state, v095_recommendation_state
from yt_playlist.core.store import Store


def test_actual_learning_state_survives_interrupt_upgrade_and_restart(request, tmp_path):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply the retained isolated actual-library copy')
    source = Path(option).resolve(strict=True)
    assert source.is_relative_to(Path(request.config.rootpath).resolve() / '.pytest_cache')
    target = tmp_path / 'final-owners.db'
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as donor, sqlite3.connect(target) as copied:
        donor.backup(copied)
    store = Store(target)
    try:
        source_version = store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
        assert 82 <= source_version <= 92
        assert store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0] >= 795000
        migrate(store.conn, registry=registry.MIGRATIONS[:92])
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 92
        store._bind_repositories()
        original = {surface: (json.loads(raw), at) for surface, raw, at in
                    store.conn.execute('SELECT surface,payload,built_at FROM rec_proposals') if surface in owner.SURFACES}
        assert set(original) == owner.SURFACES
        assert len(original['group_choice_bandit'][0]['rows']) >= 100
        assert len(original['choice_interests'][0]['contexts']) >= 10
        reports = {surface: (raw, at) for surface, raw, at in
                   store.conn.execute('SELECT surface,payload,built_at FROM rec_proposals')
                   if surface in recommendation_reports.SURFACES}
        assert 'listening_choices' in reports
        television = {surface: (json.loads(raw), at) for surface, raw, at in
                      store.conn.execute('SELECT surface,payload,built_at FROM rec_proposals')
                      if surface in tv_recommendation_state.SURFACES}
        assert set(television) == {'tv_pool', 'tv_artist_artwork'}
        saved = dict(store.conn.execute("SELECT key,value FROM settings WHERE key IN ('radio_seed_offer','rec_autotune_result')"))
        assert set(saved) == recommendation_setting_outputs.KEYS
        legacy = list(map(tuple, store.conn.execute('SELECT * FROM rec_proposals ORDER BY surface')))
        protected = ('metadata_deliveries', 'metadata_observations', 'metadata_conflicts',
                     'metadata_conflict_candidates', 'provider_attempts', 'album_descriptions',
                     'provider_usage', 'recommendation_generations', 'recommendation_pools')
        def counts():
            return {table: store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in protected}
        before = counts()
        roots = list(map(tuple, store.conn.execute('SELECT * FROM group_evidence_owners ORDER BY kind,external_key')))
        with pytest.raises(InterruptedError), store.conn.atomic():
            v093_choice_learning.apply(store.conn, None)
            v094_collection_state.apply(store.conn, SimpleNamespace(now=time.time()))
            v095_recommendation_state.apply(store.conn, None)
            raise InterruptedError('After relocation, before commit')
        assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='choice_learning_publications'").fetchone()
        assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='model_pair_heads'").fetchone()
        assert list(map(tuple, store.conn.execute('SELECT * FROM rec_proposals ORDER BY surface'))) == legacy
        assert list(map(tuple, store.conn.execute('SELECT * FROM group_evidence_owners ORDER BY kind,external_key'))) == roots
        assert {surface: (json.loads(raw), at) for surface, raw, at in
                store.conn.execute('SELECT surface,payload,built_at FROM rec_proposals') if surface in original} == original
        assert {surface: (raw, at) for surface, raw, at in
                store.conn.execute('SELECT surface,payload,built_at FROM rec_proposals') if surface in reports} == reports
        assert {key: store.conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()[0]
                for key in saved} == saved
        started = time.monotonic()
        store.init_schema()
        elapsed = time.monotonic() - started
        for surface, (value, at) in original.items():
            assert store.rec_proposals.get_proposals(surface) == value
            assert store.conn.execute('SELECT p.built_at FROM choice_learning_heads h '
                'JOIN choice_learning_publications p ON p.id=h.publication_id WHERE h.surface=?', (surface,)).fetchone()[0] == at
            assert not store.conn.execute('SELECT 1 FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
        for surface, (raw, at) in reports.items():
            assert recommendation_reports.encoded(store.conn, surface) == raw
            assert store.conn.execute('SELECT p.built_at FROM recommendation_report_heads h '
                'JOIN recommendation_report_publications p ON p.id=h.publication_id WHERE h.surface=?',
                (surface,)).fetchone()[0] == at
            assert not store.conn.execute('SELECT 1 FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
        for surface, (value, _) in television.items():
            assert store.rec_proposals.get_proposals(surface) == value
        assert {key: store.settings.get_setting(key) for key in saved} == saved
        assert not store.conn.execute("SELECT 1 FROM settings WHERE key IN ('radio_seed_offer','rec_autotune_result')").fetchone()
        assert not store.conn.execute('SELECT 1 FROM rec_proposals').fetchone()
        with pytest.raises(sqlite3.IntegrityError, match='domain owner'), store.conn.atomic():
            store.conn.execute("INSERT INTO rec_proposals VALUES ('unowned-output','{}',NULL)")
        assert counts() == before
        reopened = Store(target)
        try:
            reopened.init_schema()
            for surface, (value, _) in original.items():
                assert reopened.rec_proposals.get_proposals(surface) == value
            for surface, (raw, _) in reports.items():
                assert recommendation_reports.encoded(reopened.conn, surface) == raw
            for surface, (value, _) in television.items():
                assert reopened.rec_proposals.get_proposals(surface) == value
            assert {key: reopened.settings.get_setting(key) for key in saved} == saved
            assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
        finally:
            reopened.conn.close()
        (tmp_path / 'learning-state.json').write_text(json.dumps({
            'status': 'passed', 'isolated_source': str(source), 'isolated_target': str(target), 'python': sys.version,
            'sqlite': sqlite3.sqlite_version, 'source_schema': source_version, 'schema_before': 92, 'schema_after': current.VERSION,
            'source_digests': {surface: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
                               for surface, (value, _) in original.items()},
            'report_digests': {surface: hashlib.sha256(raw.encode()).hexdigest()
                               for surface, (raw, _) in reports.items()},
            'tv_digests': {surface: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
                           for surface, (value, _) in television.items()},
            'saved_digests': {key: hashlib.sha256(raw.encode()).hexdigest() for key, raw in saved.items()},
            'protected_counts': before, 'upgrade_seconds': elapsed, 'interrupted_cutover_rollback': True,
            'restart_verified': True, 'group_estimates': store.conn.execute('SELECT count(*) FROM choice_group_estimates').fetchone()[0],
            'contexts': store.conn.execute('SELECT count(*) FROM choice_interest_contexts').fetchone()[0],
            'features': store.conn.execute('SELECT count(*) FROM choice_interest_features').fetchone()[0]}, indent=2) + '\n')
    finally:
        store.conn.close()
