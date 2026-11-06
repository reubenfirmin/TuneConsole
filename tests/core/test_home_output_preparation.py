"""Home preparation retains exact displays and the roots already owned by them."""
from dataclasses import replace
import json

import pytest

from tests.core.test_predecessor_writes import installed
from tests.publication_timing_support import publication_costs
from yt_playlist.core import group_evidence, recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import home_outputs as preparation
from yt_playlist.core.store import Store


def finish(job, conn, size=2):
    for _ in range(100):
        progress = job.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Home preparation fixture did not finish')


def head(conn, surface='home_prepared'):
    return conn.execute(f'SELECT g.* FROM {preparation.CURRENT} h JOIN {preparation.HEAD} g '
                        'USING(surface,generation) WHERE surface=?', (surface,)).fetchone()


def seed(store):
    store.rec_ledger.record_serving_policy('real-policy', {'algorithm': 'saved'}, 1)
    documents = {
        'adaptive_group_preview': json.dumps({'schema': 2, 'generation': 'original-generation',
            'context': {'artifact': 'missing-artifact'},
            'menus': {'wheelhouse': [{'title': 'été' * 40000}], 'explore': []}}, ensure_ascii=False, indent=2),
        'home_prepared': json.dumps({'context': {'model_version': 'missing-policy'},
            'protos': [{'tracks': [{'pool_policy': 'real-policy'}]}],
            'unrelated': {'model_version': 'unrelated-name'}}, indent=2),
    }
    with store.conn.atomic():
        store.conn.executemany('INSERT INTO rec_proposals VALUES (?,?,NULL)', documents.items())
        group_evidence.retain_unknown_context(store.conn, 'prepared',
            'proposal:adaptive_group_preview:context', 'missing-artifact', 'missing_context', 123)
        group_evidence.retain(store.conn, 'prepared', 'proposal:home_prepared:choice_context',
            {'groups': [{'definition_version': 'historically-missing'}]}, 124, strict=False)
        group_evidence.retain_unknown_context(store.conn, 'prepared',
            'proposal:unrelated:context', 'unrelated-artifact', 'missing_context', 125)
    return documents


def root_rows(conn):
    return {owner: [dict(row) for row in conn.execute(f'SELECT * FROM {owner} WHERE '
        + preparation.ROOT_JOB.filters[owner].format(prefix='') + ' ORDER BY '
        + ','.join(preparation.ROOT_KEYS[owner]))] for owner in preparation.ROOT_KEYS}


@pytest.fixture
def home_inputs(installed, staged_custody):
    return staged_custody(installed, 'home-output-inputs', seed)


@pytest.mark.parametrize('installed', [79, 90], indirect=True)
def test_exact_display_parts_policies_and_existing_evidence_roots_are_additive(
        installed, home_inputs, publication_costs):
    store, _ = installed
    conn = store.conn
    original = home_inputs
    roots = root_rows(conn)
    preparation.JOB.install(conn)
    assert preparation.JOB.step(conn, batch_size=1).processed == 1
    assert head(conn, 'adaptive_group_preview') is None
    regular.migrate(conn)
    finish(preparation.JOB, conn, size=128)
    for surface, raw in original.items():
        assert preparation.read_original(conn, head(conn, surface)) == raw
        assert conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (surface,)).fetchone()[0] == raw
    row = head(conn, 'adaptive_group_preview')
    assert tuple(row[name] for name in ('built_at', 'schema_version', 'edition_key', 'mix_regions')) == (
        None, 2, 'original-generation', 1)
    assert row['part_count'] > 1
    assert [tuple(row) for row in conn.execute(f'SELECT raw_key,policy_version,unresolved_reason '
        f'FROM {preparation.POLICIES} ORDER BY raw_key')] == [
            ('missing-policy', None, 'missing_policy'), ('real-policy', 'real-policy', None)]
    preparation.ROOT_JOB.install(conn)
    finish(preparation.ROOT_JOB, conn, size=8)
    archived = [json.loads(row[0]) for row in conn.execute(f'SELECT source_snapshot FROM {preparation.ORIGINALS}')]
    assert len(archived) == sum(map(len, roots.values())) == 4
    assert all(row in archived for rows in roots.values() for row in rows)
    assert root_rows(conn) == roots
    assert not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='home_display_publications'").fetchone()
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    assert files.collect_orphans(conn) == 0
    # A new policy may resolve a current reference, never the historical roots.
    store.rec_ledger.record_serving_policy('missing-policy', {'algorithm': 'later'}, 2)
    assert preparation.JOB.progress(conn).prepared_revision is None
    assert preparation.ROOT_JOB.progress(conn).prepared_revision is None
    finish(preparation.JOB, conn, size=128)
    finish(preparation.ROOT_JOB, conn, size=8)
    current = head(conn)
    assert tuple(conn.execute(f'SELECT policy_version,unresolved_reason FROM {preparation.POLICIES} '
        "WHERE surface=? AND generation=? AND raw_key='missing-policy'", ('home_prepared', current['generation'])).fetchone()) == (
            'missing-policy', None)
    assert root_rows(conn) == roots


@pytest.mark.parametrize('installed', [90], indirect=True)
def test_large_policy_set_checkpoints_individual_references(installed):
    store, _ = installed
    value = {'tracks': [{'pool_policy': 'policy-' + str(number)} for number in range(600)]}
    store.rec_proposals.put_proposals('home_prepared', value, 10)
    preparation.JOB.install(store.conn)
    prior = 0
    for _ in range(10):
        progress = preparation.JOB.step(store.conn, batch_size=128)
        assert 0 <= progress.processed - prior <= 128
        prior = progress.processed
        if progress.status == 'prepared':
            break
    else:
        pytest.fail('Long policy list did not finish')
    assert head(store.conn)['policy_count'] == 600
    assert store.conn.execute(f'SELECT count(*) FROM {preparation.POLICIES}').fetchone()[0] == 600
    assert store.rec_proposals.get_proposals('home_prepared') == value


@pytest.mark.parametrize('installed', [90], indirect=True)
def test_partial_replacement_and_renamed_source_withdraw_only_current_heads(installed, home_inputs):
    store, _ = installed
    conn = store.conn
    original = home_inputs
    preparation.JOB.install(conn)
    preparation.JOB.step(conn, batch_size=1)
    previous = conn.execute(f'SELECT * FROM {preparation.HEAD} WHERE surface=\'adaptive_group_preview\'').fetchone()
    with conn:
        conn.execute("UPDATE rec_proposals SET payload='{}' WHERE surface='adaptive_group_preview'")
    finish(preparation.JOB, conn)
    assert head(conn, 'adaptive_group_preview')['mix_regions'] == 0
    with pytest.raises(MigrationError, match='parts are incomplete'):
        preparation.read_original(conn, previous)
    part = conn.execute(f'SELECT filename,digest,byte_size FROM {preparation.PARTS} '
                        'WHERE surface=? AND generation=? AND position=0',
                        ('adaptive_group_preview', previous['generation'])).fetchone()
    assert files.read(*part, surface='adaptive_group_preview', chunk_key='0') == original['adaptive_group_preview'][:65536]
    with conn:
        conn.execute("UPDATE rec_proposals SET surface='unrelated',payload='opaque' WHERE surface='home_prepared'")
    finish(preparation.JOB, conn)
    assert head(conn) is None
    assert conn.execute(f'SELECT 1 FROM {preparation.HEAD} WHERE surface=\'home_prepared\'').fetchone()


@pytest.mark.parametrize('installed', [90], indirect=True)
def test_roots_capture_continuing_release_without_reinterpreting_unknown_alias(installed, staged_custody_templates):
    store, _ = installed
    conn = store.conn
    original = seed(store)  # Independent uncached producer for the shared input prefix.
    template = staged_custody_templates(installed[1], 'home-output-inputs', seed)
    assert original == template.result
    assert root_rows(conn) == root_rows(template.source.conn)
    if template.source.conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE name=?", (preparation.CURRENT,)).fetchone():
        assert head(template.source.conn) is None
    preparation.JOB.install(conn)
    preparation.ROOT_JOB.install(conn)
    with pytest.raises(MigrationError, match='requires reconciled output files'):
        preparation.ROOT_JOB.step(conn)
    finish(preparation.JOB, conn)
    preparation.ROOT_JOB.step(conn, retry=True, batch_size=1)
    finish(preparation.ROOT_JOB, conn)
    original_count = conn.execute(f'SELECT count(*) FROM {preparation.ORIGINALS}').fetchone()[0]
    with conn.atomic():
        group_evidence.release(conn, 'prepared', 'proposal:home_prepared:choice_context')
        group_evidence.retain_unknown_context(conn, 'prepared', 'proposal:home_prepared:context',
            'new-missing', 'missing_context', 200)
    assert preparation.ROOT_JOB.progress(conn).prepared_revision is None
    finish(preparation.ROOT_JOB, conn)
    assert not conn.execute(f'SELECT 1 FROM {preparation.ROOTS} WHERE external_key=?',
                           ('proposal:home_prepared:choice_context',)).fetchone()
    assert conn.execute(f'SELECT source_context FROM {preparation.ROOTS} WHERE owner=? AND external_key=?',
                       ('unresolved_group_contexts', 'proposal:home_prepared:context')).fetchone()[0] == 'new-missing'
    assert conn.execute(f'SELECT count(*) FROM {preparation.ORIGINALS}').fetchone()[0] == original_count + 2


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_real_group_and_recommendation_publications_keep_their_original_retention_pins(installed):
    from tests.group_support import publish as publish_groups, recordings
    from tests.repos.test_typed_recommendation_publication import plan, publish
    from yt_playlist.core import recommendation_custody
    store, _ = installed
    conn = store.conn
    tracks = recordings([('House', 12, .3)])
    with conn:
        conn.executemany('INSERT INTO recordings(identity_key) VALUES (?)', [(track.key,) for track in tracks])
    groups = publish_groups(store, tracks)
    generation = publish(store, plan(store)).generation_id
    context = {'group_generation': groups['generation'], 'musical_groups': [
        {'version': groups['groups'][0]['version'], 'group_id': groups['groups'][0]['group_id']}]}
    store.rec_proposals.put_proposals('home_prepared', {'context': context}, 1000)
    with conn.atomic():
        group_evidence.retain(conn, 'prepared', 'proposal:home_prepared:context', context, 1000)
        recommendation_custody.retain(conn, 'prepared', 'proposal:home_prepared:recommendations', generation)
    original = root_rows(conn)
    assert original['group_evidence_generations'] and original['group_evidence_versions']
    assert original['recommendation_generation_refs']
    preparation.JOB.install(conn)
    finish(preparation.JOB, conn)
    preparation.ROOT_JOB.install(conn)
    finish(preparation.ROOT_JOB, conn, size=1)
    for owner in ('group_evidence_generations', 'group_evidence_versions', 'recommendation_generation_refs'):
        archived = [json.loads(row[0]) for row in conn.execute(f'SELECT source_snapshot FROM {preparation.ORIGINALS} WHERE owner=?', (owner,))]
        assert archived == original[owner]
    assert root_rows(conn) == original
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [90], indirect=True)
def test_oversized_root_fails_without_truncation_or_releasing_the_source(installed):
    store, _ = installed
    conn = store.conn
    store.rec_proposals.put_proposals('home_prepared', {}, 1)
    raw = 'x' * (preparation.ROOT_BYTES + 1)
    with conn.atomic():
        group_evidence.retain_unknown_context(conn, 'prepared', 'proposal:home_prepared:context', raw, 'missing_context', 1)
    preparation.JOB.install(conn)
    finish(preparation.JOB, conn)
    preparation.ROOT_JOB.install(conn)
    with pytest.raises(MigrationError, match='root exceeds its preparation byte budget'):
        finish(preparation.ROOT_JOB, conn)
    assert preparation.ROOT_JOB.progress(conn).status == 'failed'
    assert conn.execute('SELECT source_context FROM unresolved_group_contexts').fetchone()[0] == raw
    regular.migrate(conn)


@pytest.mark.parametrize('installed', [90], indirect=True)
def test_file_io_fences_source_edits_without_reserving_a_writer(installed, home_inputs, monkeypatch):
    store, _ = installed
    conn = store.conn
    preparation.JOB.install(conn)
    second = Store(store._db_path)
    publish = files.publish
    calls = []
    try:
        def edit(*args):
            assert not conn.in_transaction
            if not calls:
                with second.conn:
                    second.conn.execute("UPDATE rec_proposals SET payload='{}' WHERE surface='adaptive_group_preview'")
                calls.append(True)
            return publish(*args)
        monkeypatch.setattr(files, 'publish', edit)
        assert preparation.JOB.step(conn, batch_size=1).input_revision == 1
        assert not conn.execute(f'SELECT 1 FROM {preparation.HEAD}').fetchone()
        finish(preparation.JOB, conn)
        assert preparation.read_original(conn, head(conn, 'adaptive_group_preview')) == '{}'
    finally:
        second.conn.close()


@pytest.mark.parametrize('installed', [90], indirect=True)
def test_interrupt_and_disk_failure_preserve_checkpoint_until_explicit_retry(installed, home_inputs, monkeypatch):
    store, _ = installed
    conn = store.conn
    original = home_inputs
    preparation.JOB.install(conn)
    checkpoint = preparation.JOB.step(conn, batch_size=1)
    copy = preparation.copy
    def interrupted(*args):
        copy(*args)
        raise KeyboardInterrupt('controlled Home member interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(preparation.JOB, copy=interrupted).step(conn, batch_size=1)
    assert preparation.JOB.progress(conn) == checkpoint
    assert conn.execute(f'SELECT count(*) FROM {preparation.PARTS}').fetchone()[0] == 1
    publish = files.publish
    def failed(*args):
        raise OSError('controlled Home disk failure')
    monkeypatch.setattr(files, 'publish', failed)
    with pytest.raises(OSError, match='controlled Home disk failure'):
        preparation.JOB.step(conn, batch_size=1)
    assert preparation.JOB.progress(conn).status == 'failed'
    monkeypatch.setattr(files, 'publish', publish)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            preparation.JOB.step(reopened.conn)
        preparation.JOB.step(reopened.conn, batch_size=1, retry=True)
        finish(preparation.JOB, reopened.conn)
        assert preparation.read_original(reopened.conn, head(reopened.conn, 'adaptive_group_preview')) == original['adaptive_group_preview']
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [90], indirect=True)
@pytest.mark.parametrize('raw', ['{', '{"schema":true}', '{"generation":12}', '{"value":NaN}'])
def test_invalid_display_fails_durably_without_blocking_regular_ddl(installed, raw):
    store, _ = installed
    with store.conn:
        store.conn.execute('INSERT INTO rec_proposals VALUES (?, ?, 1)', ('home_prepared', raw))
    preparation.JOB.install(store.conn)
    with pytest.raises((MigrationError, ValueError)):
        finish(preparation.JOB, store.conn)
    assert preparation.JOB.progress(store.conn).status == 'failed'
    assert store.conn.execute("SELECT payload FROM rec_proposals WHERE surface='home_prepared'").fetchone()[0] == raw
    regular.migrate(store.conn)
