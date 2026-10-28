"""Album familiarity preserves account/day fallback and recording deduplication."""


def test_summary_counts_exact_generated_and_other_account_fallback_without_duplicates(store):
    first = store.identities.upsert_identity('Main', 'main', None, True)
    second = store.identities.upsert_identity('Other', 'other', 'brand', False)
    entry = store.tracks.upsert_track('v', 'One', 'Artist', 'Release', 100, album_browse_id='B')
    key = store.tracks.catalog_subject(entry)['identity_key']
    store.collection.cache_album_tracklist('B', 'Release', 'Artist', None, [],
        entries=[{'entry_id': entry, 'track_number': 1}, {'entry_id': entry, 'track_number': 2}])
    other = store.tracks.upsert_track('other', 'Other song', 'Artist', 'Other release', 100,
                                    album_browse_id='OTHER')
    other_key = store.tracks.catalog_subject(other)['identity_key']
    day = 86400
    for date in (day, 2 * day, 3 * day):
        store.history.record_history_plays(first, date, [(key, 'today')], source_timezone='UTC')
    store.history.record_history_plays(second, 2 * day, [(key, 'today')], source_timezone='UTC')
    store.history.record_history_plays(first, 2 * day, [(other_key, 'today')], source_timezone='UTC')
    for at in (2 * day + 3600, 2 * day + 7200):
        store.history.record_play_event(first, key, 'v', at, source_timezone='UTC')
    store.history.record_play_event(first, key, 'v', 3 * day + 3600,
                                    source_timezone='UTC', context_choice=True)
    # One fallback day + two exact plays + one generated exact play + the
    # second account's fallback day. Repeated release occurrences count once.
    expected = {'B': {'plays': 5, 'tracks': 1, 'complete': True},
                'OTHER': {'plays': 1, 'tracks': 1, 'complete': False},
                'missing': {'plays': 0, 'tracks': 0, 'complete': False}}
    assert store.collection.album_listening_summary(iter(['B', 'OTHER', 'B', 'missing'])) == expected
    assert store.collection.album_listening_counts(expected) == {
        browse: row['plays'] for browse, row in expected.items()}
    assert store.collection.album_listening_summary([]) == {}
