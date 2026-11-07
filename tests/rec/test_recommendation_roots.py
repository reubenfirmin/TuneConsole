"""Actual capture, file-ledger and prepared-menu publication lifetimes."""
from contextlib import contextmanager
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.core.test_compact_groups_migration import predecessor
from tests.repos.test_compact_group_publication import database
from tests.repos.test_typed_recommendation_publication import recommendations, publish, plan, collect
from yt_playlist.core import group_documents
from yt_playlist.rec import choice_evidence, group_roots, recommendation_roots
from yt_playlist.rec.choice_capture import CaptureQueue
from yt_playlist.core.activity import WorkBudgetExceeded


def options(generation):
    return [{'id': 'one', 'score': .9, 'label': 'Your wheelhouse',
             'intent': 'familiar', 'genres': ['House', 'Techno'], 'tracks': [
        {'key': 'one', 'serving_context': {'recommendation_generation_id': generation,
                                         'recommendation_pool_id': 1, 'recommendation_pool_rank': 0}}]},
        {'id': 'two', 'score': .7, 'label': 'Explore',
         'intent': 'discovery', 'genres': ['Ambient'], 'tracks': []}]


def issue(store, generation, identity='menu'):
    return choice_evidence.issue(store, 'mixes', options(generation), 10,
                                 context={'at': 10}, opportunity_id=identity, reuse=True)


def roots(store, kind):
    return [tuple(row) for row in store.conn.execute('SELECT external_key,generation_id '
        'FROM recommendation_generation_refs WHERE kind=? ORDER BY external_key,generation_id', (kind,))]


def exists(store, generation):
    return store.conn.execute('SELECT 1 FROM recommendation_generations WHERE id=?', (generation,)).fetchone() is not None


def test_reader_selects_and_retains_the_head_atomically_until_idempotent_close(recommendations, monkeypatch):
    store = recommendations
    first = publish(store, plan(store)).generation_id
    selecting, switched = threading.Event(), threading.Event()
    writer = group_roots.writer

    @contextmanager
    def before_selection(source):
        # Replace the head after the fallback's presence check, before the
        # transaction which must select and retain the current publication.
        selecting.set()
        assert switched.wait(5)
        with writer(source) as conn:
            yield conn

    def replacement():
        try:
            assert selecting.wait(5)
            second = publish(store, plan(store, 'new-head')).generation_id
            collect(store)
            assert not exists(store, first)
            return second
        finally:
            switched.set()

    close = None
    try:
        with ThreadPoolExecutor(1) as executor:
            worker = executor.submit(replacement)
            with monkeypatch.context() as patch:
                patch.setattr(group_roots, 'writer', before_selection)
                generation, close = group_roots.pending_head(store, ('radio', 'home'), 10)
            second = worker.result(2)
        assert generation == second and close is not None
        assert [value for _, value in roots(store, 'pending')] == [second]
        publish(store, plan(store, 'third-head'))
        group_roots.reap_pending(store)
        collect(store)
        assert exists(store, second)
    finally:
        if close is not None:
            close()
            close()
    assert not roots(store, 'pending')
    collect(store)
    assert not exists(store, second)


@pytest.mark.parametrize('query_only', [False, True], ids=['writable', 'paused'])
def test_reader_without_a_head_leaves_no_pending_sql_or_lifetime_file(recommendations, query_only):
    store = recommendations
    statements = []
    store.conn.execute(f'PRAGMA query_only={int(query_only)}')
    store.conn.set_trace_callback(statements.append)
    try:
        assert group_roots.pending_head(store, ('radio', 'home'), 10) == (None, None)
    finally:
        store.conn.set_trace_callback(None)
        store.conn.execute('PRAGMA query_only=OFF')
    assert not any(sql.startswith(('BEGIN', 'INSERT', 'UPDATE', 'DELETE')) for sql in statements)
    assert not roots(store, 'pending')
    assert not store.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='pending'").fetchone()
    assert not list(store._db_path.parent.glob(store._db_path.name + '.groups/.pending/*.lock'))


@pytest.mark.parametrize('has_head', [False, True])
def test_sql_reader_during_evidence_publication_retains_its_head_until_reaped(recommendations, has_head):
    store = recommendations
    directory = group_documents.directory(store.conn) / '.pending'
    first, close = None, None
    if has_head:
        first = publish(store, plan(store)).generation_id
    entered, release = threading.Event(), threading.Event()

    def publication():
        with group_roots.admission(store):
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(1) as executor:
        worker = executor.submit(publication)
        try:
            assert entered.wait(2)
            # Actual historical file evidence still needs its domain guard.
            with pytest.raises(WorkBudgetExceeded, match='using publication'):
                group_roots.pending(store, {'group_generation': 'second'}, 10)
            assert not list(directory.glob('*.lock'))
            changes = store.conn.total_changes
            generation, close = group_roots.pending_head(store, ('radio', 'home'), 10)
            if has_head:
                assert generation == first and close is not None
                retained = roots(store, 'pending')
                assert len(retained) == 1 and retained[0][1] == first
                assert len(list(directory.glob('*.lock'))) == 1
                publish(store, plan(store, 'new-head'))
                collect(store)
                assert exists(store, first)
                close()
                close()
                # Busy evidence admission defers retirement. The released OS
                # lifetime lets a later bounded reaper finish the SQL owner.
                assert roots(store, 'pending') == retained
                assert len(list(directory.glob('*.lock'))) == 1
                collect(store)
                assert exists(store, first)
            else:
                assert (generation, close) == (None, None)
                assert store.conn.total_changes == changes
                assert not roots(store, 'pending')
                assert not list(directory.glob('*.lock'))
        finally:
            try:
                if close is not None:
                    close()
            finally:
                release.set()
        worker.result(2)
    group_roots.reap_pending(store)
    assert not roots(store, 'pending')
    assert not store.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='pending'").fetchone()
    assert not list(directory.glob('*.lock'))
    if close is not None:
        close()
    collect(store)
    if has_head:
        assert not exists(store, first)
        assert store.recommendations.head('home') is not None


def test_private_reader_selects_its_pinned_head_after_the_writer_switches_and_retires_it(recommendations):
    store = recommendations
    first = publish(store, plan(store)).generation_id
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        assert reader.recommendations.head('home')['id'] == first
        second = publish(store, plan(store, 'new-head')).generation_id
        collect(store)
        assert not exists(store, first)
        assert group_roots.pending_head(reader, ('radio', 'home'), 12) == (first, None)
        assert reader.recommendations.output('home', 'framing', generation=first) == {'title': 'one'}
        assert reader.recommendations.page('home', 'all', 'wheelhouse', generation=first)[0]['key'] == 'one'
        assert not roots(store, 'pending')
    generation, close = group_roots.pending_head(store, ('home',), 12)
    try:
        assert generation == second
    finally:
        close()


@pytest.mark.parametrize('explicit_generation', [False, True])
def test_radio_during_maintenance_defers_its_lease_until_worker_resume(recommendations, explicit_generation):
    from yt_playlist.rec.stored_radio import PreparedRadio

    store = recommendations
    first = publish(store, plan(store)).generation_id
    store.activity_paused = True
    store.conn.execute('PRAGMA query_only=ON')
    changes = store.conn.total_changes
    prepared = None
    try:
        prepared = PreparedRadio(store, 12, generation=first if explicit_generation else None)
        assert prepared.generation == first
        assert store.recommendations.output('home', 'framing', generation=prepared.generation) == {'title': 'one'}
        prepared.retain(12)
        assert not roots(store, 'pending')
        assert store.conn.total_changes == changes
    finally:
        store.conn.execute('PRAGMA query_only=OFF')
        store.activity_paused = False
    try:
        # The app's existing resume callback does this before starting workers.
        prepared.retain(13)
        prepared.retain(13)
        assert [generation for _, generation in roots(store, 'pending')] == [first]
        publish(store, plan(store, 'after-maintenance'))
        collect(store)
        assert exists(store, first)
        assert store.recommendations.output('home', 'framing', generation=prepared.generation) == {'title': 'one'}
    finally:
        if prepared is not None:
            prepared.close()
    collect(store)
    assert not exists(store, first)


def test_queued_mix_capture_and_late_response_survive_retiring_track_generation(recommendations):
    store = recommendations
    first = publish(store, plan(store)).generation_id
    jobs = []
    store.choice_capture = CaptureQueue(lambda job: jobs.append(job) or True)
    assert issue(store, first) == 'menu'
    assert not roots(store, 'pending')
    assert not store.rec_ledger.evidence().get('opportunity', 'menu')
    assert choice_evidence.respond(store, 'menu', 'choice', 11, option_id='one')
    assert not roots(store, 'pending')
    publish(store, plan(store, 'second'))
    collect(store)
    assert not exists(store, first)
    assert jobs[0].run() == 'menu'
    jobs[0].close()
    offer = store.rec_ledger.evidence().get('opportunity', 'menu')
    assert [(o['id'], o['label'], o['intent'], o['genres']) for o in offer['options']] == [
        ('one', 'Your wheelhouse', 'familiar', ['House', 'Techno']),
        ('two', 'Explore', 'discovery', ['Ambient'])]
    assert all('tracks' not in o and 'serving_context' not in o for o in offer['options'])
    assert not roots(store, 'offer')
    assert jobs[1].run()
    jobs[1].close()
    assert not roots(store, 'pending')
    response = store.rec_ledger.evidence().get('response', 'menu:choice:one')
    assert response['option_id'] == 'one' and response['at'] == 11
    assert not roots(store, 'response')
    collect(store)
    assert not exists(store, first)


def test_cancelled_duplicate_mix_capture_does_not_release_an_active_reader(recommendations):
    store = recommendations
    first = publish(store, plan(store)).generation_id
    generation, close_reader = group_roots.pending_head(store, ('home',), 10)
    assert generation == first
    jobs = []
    store.choice_capture = CaptureQueue(lambda job: jobs.append(job) or True)
    assert issue(store, first) == issue(store, first) == 'menu'
    assert len(jobs) == len(roots(store, 'pending')) == 1
    group_roots.reap_pending(store)
    assert len(roots(store, 'pending')) == 1
    publish(store, plan(store, 'second'))
    collect(store)
    assert exists(store, first)
    jobs[0].close()
    jobs[0].close()
    collect(store)
    assert exists(store, first)
    assert len(roots(store, 'pending')) == 1
    close_reader()
    close_reader()
    collect(store)
    assert not exists(store, first)
    assert not roots(store, 'pending') and not roots(store, 'offer')


def test_actual_file_retirement_expires_compact_offer_without_pinning_track_publication(recommendations):
    store = recommendations
    first = publish(store, plan(store)).generation_id
    assert issue(store, first) == 'menu'
    publish(store, plan(store, 'second'))
    collect(store)
    assert not exists(store, first)
    assert store.rec_ledger.evidence().get('opportunity', 'menu') is not None
    assert not roots(store, 'offer')
    with group_roots.admission(store):
        retired = store.rec_ledger.evidence().maintain(100, orphan_before=100,
            retire_references=lambda offers, checkpoints: group_roots.retired(store, offers, checkpoints))
    assert retired['retired_unanswered'] == 1
    assert not roots(store, 'offer')
    collect(store)
    assert not exists(store, first)


def test_prepared_menus_retain_track_generations_with_atomic_replacement_and_sql_deletion(recommendations):
    store = recommendations
    first = publish(store, plan(store)).generation_id
    data = {'menus': [{'protos': options(first)}]}
    store.rec_proposals.put_proposals('home_prepared', data, 10)
    second = publish(store, plan(store, 'second')).generation_id
    replacement = {'protos': options(second)}
    with pytest.raises(RuntimeError, match='controlled rollback'), store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        store.rec_proposals.publish_prepared_home(json.dumps(replacement), 11)
        raise RuntimeError('controlled rollback')
    assert roots(store, 'prepared') == [('proposal:home_prepared:recommendations', first)]
    assert store.rec_proposals.get_proposals('home_prepared') == data
    collect(store)
    assert exists(store, first)
    store.rec_proposals.put_proposals('home_prepared', replacement, 12)
    assert roots(store, 'prepared') == [('proposal:home_prepared:recommendations', second)]
    collect(store)
    assert not exists(store, first)
    with store.conn:
        store.conn.execute("DELETE FROM rec_proposals WHERE surface='home_prepared'")
    assert not roots(store, 'prepared')


def test_comparison_context_retains_publication_without_decoding_compressed_payload(recommendations):
    store = recommendations
    first = publish(store, plan(store)).generation_id
    data = {'codec': 'unreadable-compressed-payload', 'data': 'opaque',
            'context': {'recommendation_generations': [first]}}
    surface = 'model_pair:trial:incumbent:mixes'
    store.rec_proposals.put_proposals(surface, data, 10)
    assert roots(store, 'comparison') == [('proposal:' + surface + ':context', first)]
    publish(store, plan(store, 'second'))
    collect(store)
    assert exists(store, first)
    with store.conn:
        store.conn.execute('DELETE FROM rec_proposals WHERE surface=?', (surface,))
    collect(store)
    assert not exists(store, first)


def test_nonexistent_publication_is_rejected_for_readers_but_not_retained_by_mix_capture(recommendations):
    store = recommendations
    jobs = []
    store.choice_capture = CaptureQueue(lambda job: jobs.append(job) or True)
    with pytest.raises(sqlite3.IntegrityError):
        group_roots.pending(store, {'recommendation_generations': [999999]}, 10)
    assert not jobs and not roots(store, 'pending')
    assert not store.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='pending'").fetchone()
    assert issue(store, 999999) == 'menu'
    assert len(jobs) == 1 and not roots(store, 'pending')
    assert jobs[0].run() == 'menu'
    jobs[0].close()
    offer = store.rec_ledger.evidence().get('opportunity', 'menu')
    assert all('tracks' not in option for option in offer['options'])
    assert not roots(store, 'offer')


@pytest.mark.parametrize('value', [None, {}, 'opaque'])
def test_malformed_prepared_options_preserve_the_existing_root(recommendations, value):
    store = recommendations
    first = publish(store, plan(store)).generation_id
    data = {'protos': options(first)}
    store.rec_proposals.put_proposals('home_prepared', data, 10)
    with pytest.raises(ValueError):
        store.rec_proposals.put_proposals('home_prepared', {'protos': value}, 11)
    assert store.rec_proposals.get_proposals('home_prepared') == data
    assert roots(store, 'prepared') == [('proposal:home_prepared:recommendations', first)]


def test_references_use_only_declared_track_context_and_never_recursive_json():
    nested = {'recommendation_generation_id': 999}
    assert recommendation_roots.from_options([{'tracks': [{'arbitrary': nested}], 'policy': nested}]) == []
    assert recommendation_roots.references({'layers': nested, 'policy': nested}) == []
    with pytest.raises(ValueError, match='Invalid served'):
        recommendation_roots.from_options(options(True))


@pytest.mark.parametrize('shape', ['held_menu', 'album_companions', 'direct_tracks'])
def test_display_track_boundaries_retain_the_served_generation(recommendations, shape):
    store = recommendations
    first = publish(store, plan(store)).generation_id
    tracks = options(first)[0]['tracks']
    data = ({'menu': {'protos': options(first)}} if shape == 'held_menu' else
            {'items': [{'companion': tracks}]} if shape == 'album_companions' else {'tracks': tracks})
    store.rec_proposals.put_proposals('home_choice', data, 10)
    publish(store, plan(store, 'second'))
    collect(store)
    assert roots(store, 'prepared') == [('proposal:home_choice:recommendations', first)]
    assert exists(store, first)
