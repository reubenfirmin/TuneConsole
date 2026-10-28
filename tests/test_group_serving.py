from fastapi.testclient import TestClient

from tests.group_support import publish, recordings
from yt_playlist.rec import group_serving
from yt_playlist.web.app import create_app


def test_preview_wait_survives_missing_head_and_budget_then_clears_on_publication(store, monkeypatch):
    from yt_playlist.core.activity import WorkBudgetExceeded
    from yt_playlist.core.work_diagnostics import MaintenanceDiagnostics, work_span
    clock = [0.]
    store.maintenance_diagnostics = MaintenanceDiagnostics(clock=lambda: clock[0])
    assert group_serving.prepare(store, 1000) is None
    assert store.maintenance_diagnostics.waiting == {group_serving.PHASE: 0.}
    tracks = recordings([('House', 20, .4)])
    publish(store, tracks)
    pools(store, tracks)
    calculate = group_serving.calculate

    def exhausted(*args):
        raise WorkBudgetExceeded('preview unfinished')

    monkeypatch.setattr(group_serving, 'calculate', exhausted)
    clock[0] = 20.
    assert group_serving.prepare(store, 1100) is None
    assert store.maintenance_diagnostics.waiting == {group_serving.PHASE: 0.}
    monkeypatch.setattr(group_serving, 'calculate', calculate)
    with work_span('rec: adaptive grouping shadow'):
        assert group_serving.prepare(store, 1200)
    assert not store.maintenance_diagnostics.waiting
    assert store.maintenance_diagnostics.phases[group_serving.PHASE, 'completed'] == 1


def test_disabling_preview_retires_wait_without_removing_last_good_payload(store):
    store.rec_proposals.put_proposals(group_serving.SURFACE, {'last-good': True}, 1000)
    store.maintenance_diagnostics.record('deferred', phase=group_serving.PHASE)
    store.settings.set_setting('rec_group_shadow', '0')
    assert group_serving.prepare(store, 1100) is None
    assert group_serving.prepare(store, 1200) is None
    assert not store.maintenance_diagnostics.waiting
    assert store.maintenance_diagnostics.reasons[group_serving.PHASE, 'disabled'] == 1
    assert store.rec_proposals.get_proposals(group_serving.SURFACE) == {'last-good': True}


def hidden_ancestor_preview(store):
    """Collapsed naming nodes retain their versioned hierarchy in prepared data."""
    rows = [
        ('house', None, 'House', 1163, True),
        ('hidden-house', 'house', 'House', 974, False),
        ('hidden-house-2', 'hidden-house', 'House', 500, False),
        ('tech-house', 'hidden-house-2', 'Tech House', 134, True),
        ('house-leaf', 'hidden-house', 'House', 191, True),
        ('rock', None, 'Classic Rock', 600, True),
        ('hidden-rock', 'rock', 'Rock', 500, False),
        ('prog-rock', 'hidden-rock', 'Progressive Rock', 79, True),
        ('classic-rock', 'hidden-rock', 'Classic Rock', 55, True),
        ('orphan', 'missing', 'Jazz', 30, True),
    ]
    preview = {'generation': 'collapsed-preview', 'counts': {'known': 1793, 'unknown': 0},
               'groups': [{'version': version, 'parent_version': parent, 'name': name,
                           'size': size, 'display': display, 'frames': {}}
                          for version, parent, name, size, display in rows]}
    store.rec_proposals.put_proposals(group_serving.SURFACE, preview, 1000)
    return preview


def test_view_preserves_visible_ancestry_through_hidden_names_without_mutating_preview(store, monkeypatch):
    preview = hidden_ancestor_preview(store)
    monkeypatch.setattr(store.taste_groups, 'head_generation', lambda: preview['generation'])
    shown = {group['version']: group for group in group_serving.view(store)['groups']}
    assert shown['tech-house']['parent_name'] == shown['house-leaf']['parent_name'] == 'House'
    assert shown['prog-rock']['parent_name'] == shown['classic-rock']['parent_name'] == 'Classic Rock'
    assert shown['house']['parent_name'] is None and shown['rock']['parent_name'] is None
    assert shown['orphan']['parent_name'] is None
    assert 'hidden-house' not in shown and 'hidden-house-2' not in shown and 'hidden-rock' not in shown
    assert shown['tech-house']['parent_version'] == 'hidden-house-2'
    assert store.rec_proposals.get_proposals(group_serving.SURFACE) == preview


def pools(store, tracks, *, one_album=False):
    pool = [{'key': r.key, 'title': f'Song {i}', 'video_id': f'v{i}', 'artist': f'Artist {i // 2}',
             'album': 'One album' if one_album else f'Album {i // 2}'} for i, r in enumerate(tracks)]
    store.rec_proposals.put_proposals('mode_bundles', {'all': {'wheelhouse': pool, 'fresh': pool}}, 1000)


def test_full_mix_requires_twelve_actual_diverse_eligible_members(store):
    tracks = recordings([('House', 20, .4)])
    head = publish(store, tracks)
    pools(store, tracks, one_album=True)
    first = group_serving.calculate(store, head, 1100)
    assert not first['groups'][0]['frames']['wheelhouse']['full_mix']
    assert first['groups'][0]['frames']['wheelhouse']['eligible'] == 2
    pools(store, tracks)
    second = group_serving.calculate(store, head, 1100)
    mix = second['groups'][0]['frames']['wheelhouse']
    assert mix['full_mix'] and len(mix['tracks']) == 12
    assert {item['key'] for item in mix['tracks']} <= set(head['groups'][0]['members'])


def test_minority_album_is_represented_even_when_it_cannot_fill_a_mix(store):
    tracks = recordings([('Death Metal', 80, .9), ('Pop', 12, .2)])
    head = publish(store, tracks)
    pools(store, tracks, one_album=True)
    preview = group_serving.calculate(store, head, 1100)
    pop = next(group for group in preview['groups'] if group['name'] == 'Pop')
    assert pop['size'] == 12
    assert not pop['frames']['wheelhouse']['full_mix']
    assert pop['frames']['wheelhouse']['fallback_version'] is None  # No unrelated global-root backfill.


def test_child_falls_back_to_supported_parent_without_inventing_a_child_mix(store):
    tracks = recordings([('House', 10, .1), ('House', 10, .95)])
    head = publish(store, tracks)
    pools(store, tracks)
    preview = group_serving.calculate(store, head, 1100)
    root = preview['groups'][0]
    assert root['frames']['wheelhouse']['full_mix']
    for child in preview['groups'][1:]:
        assert not child['frames']['wheelhouse']['full_mix']
        assert child['frames']['wheelhouse']['fallback_version'] == root['version']


def test_prepared_preview_has_exact_versions_and_never_creates_shadow_offers(store):
    tracks = recordings([('House', 20, .4)])
    head = publish(store, tracks)
    pools(store, tracks)
    before = list(store.rec_ledger.evidence().records('opportunity'))
    preview = group_serving.prepare(store, 1100)
    assert preview['generation'] == head['generation']
    assert preview['context']['musical_groups'][0]['version'] == head['groups'][0]['version']
    assert list(store.rec_ledger.evidence().records('opportunity')) == before
    assert group_serving.view(store)['groups'][0]['mix_ready']
    publish(store, tracks, 1200, head)
    retained = group_serving.view(store)
    assert retained['updating'] and retained['generation'] == preview['generation']
    assert retained['context'] == preview['context']
    updated = group_serving.prepare(store, 1300)
    assert group_serving.view(store)['generation'] == updated['generation']
    assert not group_serving.view(store)['updating']


def test_taste_preview_uses_prepared_names_without_loading_or_fitting_membership(store, monkeypatch):
    tracks = recordings([('House', 20, .1), ('House', 20, .95)])
    publish(store, tracks)
    pools(store, tracks)
    group_serving.prepare(store, 1100)
    from yt_playlist.rec import group_discovery
    monkeypatch.setattr(group_discovery, 'discover', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('foreground fit')))
    app = create_app(store, lambda *a, **kw: None, now_fn=lambda: 1200)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.get('/taste/grouping-preview')
        assert response.status_code == 200
        assert 'Instrumental house' in response.text and 'Vocal house' in response.text
        assert 'data-group-version=' in response.text


def test_progress_separates_recordings_from_source_jobs_and_explains_failures(store):
    from yt_playlist.rec import group_preparation
    with store.conn:
        store.conn.execute("INSERT INTO rec_content_pending(identity_key) VALUES ('pending')")
        store.conn.execute("INSERT INTO rec_musical_sources VALUES ('source','test','artist','artist','{}',1)")
    group_preparation.report(store, 'failed', 1000, reason='preparation-failed')
    app = create_app(store, lambda *a, **kw: None, now_fn=lambda: 1200)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.get('/taste/grouping-preview')
    assert '0 library recordings prepared; 1 recording queued for preparation' in response.text
    assert '1 artist detail update' in response.text
    assert 'could not be updated' in response.text
    assert 'retry automatically' in response.text
    assert 'Your current listening choices remain available' not in response.text
