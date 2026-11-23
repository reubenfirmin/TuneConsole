"""Provider artist entities retain provenance and enrich normalized catalog credits."""
import pytest

from yt_playlist.providers import artist_credits, musicbrainz, lastfm, discogs, deezer
from yt_playlist.providers.waterfall import TrackSink


@pytest.fixture(params=['native', 'runtime'])
def credit_store(request):
    store = request.getfixturevalue('file_backed_store')
    if request.param == 'runtime':
        from yt_playlist.core import metadata_runtime
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            metadata_runtime.activate(store.conn)
    return store


def test_musicbrainz_credit_requires_consistent_explicit_entities():
    credit = [{'name': 'Rita Payés', 'artist': {'id': 'rita'}},
              ' & ', {'artist': {'name': 'Elisabeth Roma', 'id': 'elisabeth'}}]
    rec = {'artist-credit': credit}
    assert [row['id'] for row in artist_credits.musicbrainz([rec, rec])] == ['rita', 'elisabeth']
    assert not artist_credits.musicbrainz([rec, {'artist-credit': credit[:1]}])
    assert not artist_credits.musicbrainz([{'artist-credit': [{'name': 'Rita & Elisabeth'}]}])


def test_deezer_contributors_exclude_production_roles_and_preserve_literal_band_names():
    track = {'contributors': [{'name': 'Earth, Wind & Fire', 'id': 1, 'role': 'Main'},
                             {'name': 'Guest', 'id': 2, 'role': 'Featured'},
                             {'name': 'Producer', 'id': 3, 'role': 'Producer'}]}
    assert [row['name'] for row in artist_credits.deezer(track)] == ['Earth, Wind & Fire', 'Guest']
    track['contributors'][1]['role'] = 'Unknown'
    assert not artist_credits.deezer(track)


def test_discogs_track_credit_overrides_release_artist_and_ignores_extraartists():
    release = {'artists': [{'name': 'Various', 'id': 194}], 'tracklist': [
        {'title': 'Caricias', 'artists': [{'name': 'Rita Payés', 'id': 1}, {'name': 'Elisabeth Roma', 'id': 2}],
         'extraartists': [{'name': 'Producer', 'id': 3}]}]}
    assert [row['id'] for row in artist_credits.discogs(release, 'Caricias')] == ['1', '2']
    assert not artist_credits.discogs(release, 'Another song')


def test_musicbrainz_probe_exposes_verified_structured_credit(monkeypatch):
    rec = {'title': 'Caricias', 'artist-credit': [
        {'artist': {'name': 'Rita Payés', 'id': 'rita'}, 'joinphrase': ' & '},
        {'artist': {'name': 'Elisabeth Roma', 'id': 'elisabeth'}}]}
    monkeypatch.setattr(musicbrainz, '_recording_search', lambda *args: [rec])
    monkeypatch.setattr(musicbrainz, '_genre_year', lambda *args, **kwargs: (None, None))
    result = musicbrainz.probe({'title': 'Caricias', 'artist': 'Rita Payés & Elisabeth Roma'})
    assert len(result.fields['artist_credits']) == 2


def test_lastfm_uses_mbid_without_splitting_a_combined_name(monkeypatch):
    monkeypatch.setattr(lastfm, '_get', lambda *args, **kwargs: {'track': {
        'name': 'Song', 'artist': {'name': 'Earth, Wind & Fire', 'mbid': 'band'},
        'toptags': {'tag': [{'name': 'Funk'}]}}})
    result = lastfm.probe({'title': 'Song', 'artist': 'Earth, Wind & Fire'}, key='fixture')
    assert result.fields['artist_credits'] == [
        {'name': 'Earth, Wind & Fire', 'id': 'band', 'provider': 'musicbrainz'}]


def apply(store, entry, provider, credits, *, at=10):
    track = store.tracks.track_for_waterfall(entry)
    sink = TrackSink(store, track)
    fields = {'artist_credits': credits}
    sink.capture_result('credits-' + str(at), provider, fields, at)
    sink.apply_fields(provider, fields)
    return sink


def test_enrichment_preserves_youtube_identities_and_records_both_provider_ids(credit_store):
    store = credit_store
    entry = store.tracks.upsert_track('caricias', 'Caricias', 'Rita Payés, Elisabeth Roma', credits=[
        {'name': 'Rita Payés', 'id': 'UC-rita'}, {'name': 'Elisabeth Roma', 'id': 'UC-elisabeth'}])
    before = [row['artist_id'] for row in store.artists.track_credits(entry)]
    credits = [{'name': 'Rita Payés', 'id': 'mb-rita'}, {'name': 'Elisabeth Roma', 'id': 'mb-elisabeth'}]
    sink = apply(store, entry, 'musicbrainz', credits)
    assert [row['artist_id'] for row in store.artists.track_credits(entry)] == before
    assert [store.artists.repository.provider_id('musicbrainz', member['id']) for member in credits] == before
    # Replay is an existing capture, not another identity or observation.
    count = store.conn.execute('SELECT count(*) FROM artist_credit_observations').fetchone()[0]
    sink.apply_fields('musicbrainz', {'artist_credits': credits})
    assert store.conn.execute('SELECT count(*) FROM artist_credit_observations').fetchone()[0] == count
    assert len(store.rec_query.tracks_by_keys([sink.track['identity_key']])[sink.track['identity_key']]['artist_ids']) == 2


def test_primary_only_result_cannot_erase_a_collaborator(credit_store):
    store = credit_store
    entry = store.tracks.upsert_track('duet', 'Duet', 'Rita & Elisabeth', credits=[
        {'name': 'Rita', 'id': 'UC-rita'}, {'name': 'Elisabeth', 'id': 'UC-elisabeth'}])
    before = [row['artist_id'] for row in store.artists.track_credits(entry)]
    apply(store, entry, 'lastfm', [{'name': 'Rita', 'id': 'mb-rita', 'provider': 'musicbrainz'}])
    assert [row['artist_id'] for row in store.artists.track_credits(entry)] == before


def test_explicit_collaboration_refines_one_unresolved_combined_credit(credit_store):
    store = credit_store
    entry = store.tracks.upsert_track('duet', 'Duet', 'Rita & Elisabeth')
    apply(store, entry, 'musicbrainz', [{'name': 'Rita', 'id': 'mb-rita'}, {'name': 'Elisabeth', 'id': 'mb-elisabeth'}])
    assert [row['credit'] for row in store.artists.track_credits(entry)] == ['Rita', 'Elisabeth']


def test_same_name_conflicting_verified_identities_are_not_merged(credit_store):
    store = credit_store
    other = store.artists.ensure_artist('Rita', provider='musicbrainz', provider_id='different-rita')
    entry = store.tracks.upsert_track('solo', 'Solo', 'Rita', credits=[{'name': 'Rita', 'id': 'UC-rita'}])
    before = store.artists.track_credits(entry)[0]['artist_id']
    apply(store, entry, 'musicbrainz', [{'name': 'Rita', 'id': 'different-rita'}])
    assert store.artists.track_credits(entry)[0]['artist_id'] == before != other


def test_discogs_probe_fetches_matched_tracklist_for_credit_only(monkeypatch):
    monkeypatch.setattr(discogs, '_search', lambda *args: [
        {'id': 10, 'title': 'Unrelated - Caricias'}, {'id': 20, 'title': 'Rita - Caricias'}])
    calls = []
    def details(identity, **kwargs):
        calls.append(identity)
        return {'tracklist': [{'title': 'Caricias', 'artists': [{'name': 'Rita', 'id': 1},
                                                             {'name': 'Elisabeth', 'id': 2}]}]}
    monkeypatch.setattr(discogs, 'release_details', details)
    result = discogs.probe({'title': 'Caricias', 'artist': 'Rita'}, tok='fixture')
    assert calls == [20]
    assert [row['id'] for row in result.fields['artist_credits']] == ['1', '2']
