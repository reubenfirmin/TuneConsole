"""Publish controlled candidate rankings through the ordinary current owner."""
from copy import deepcopy
import json

from tests.conftest import seed_catalog_track
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.rec import measurement, mode_surfaces


def seed_compact_group_history(store):
    """Two real published group generations below recommendation consumers."""
    with store.conn:
        store.conn.executemany('INSERT INTO recordings(id,identity_key) VALUES (?,?)', [(1, 'one'), (2, 'two')])
    for generation, at in (('first', 1), ('second', 2)):
        groups = []
        for key, parent, members in (
                ('parent', None, [('one', .9, .8), ('two', .7, .6)]),
                ('leaf', generation + '-parent', [('one', .9, .8)])):
            groups.append(dict(group_id=key, version=generation + '-' + key,
                parent_version=parent, legacy_mode_id=None, members=members,
                payload=json.dumps(dict(name=key, leaf=key == 'leaf', legacy=False,
                    membership_known=True, size=len(members), depth=int(key == 'leaf'),
                    rep_keys=['one'], naming_policy='naming-1', genres=[['House', 1.0]]))))
        prepared = dict(generation=generation, policy='{"algorithm":"grouping-1"}',
            fingerprint='input-fingerprint', at=at, serial=at, groups=groups,
            payload=json.dumps(dict(recording_inputs={
                'one': '1' * 64, 'two': '2' * 64, 'unknown-old-key': '3' * 64},
                last_structure_at=1, diagnostics={'at': at})),
            unknown=[('unknown-old-key', 'unmeasured')],
            edges=[('one', 'two', .8, .7), ('one', 'unknown-old-key', .6, .5)],
            lineage=[('first-leaf', 'second-leaf', 'continuation', json.dumps(dict(
                old_share=1, new_share=1, overlap=1, compatible=True)))] if at == 2 else [],
            _stage_parent='first' if at == 2 else None)
        store.taste_groups.publish(prepared)
        with store.conn:
            store.conn.execute('INSERT INTO group_assignments(generation_id,recording_id,group_version_id,'
                'similarity,support,profile_digest,prepared_at) SELECT g.id,2,a.group_version_id,.7,.6,?,? '
                'FROM group_generations g JOIN group_version_aliases a ON a.external_version=? '
                'WHERE g.external_key=?', ('profile-digest', at, generation + '-leaf', generation))


def seed_publication_subjects(store, items, *, discoveries=(), unowned=(), now=1):
    """Batch completed catalog inputs for publication/read coordination tests.

    These owners require real catalog membership, identities and metadata,
    rather than provider ingestion, artist inference or metadata delivery files.
    """
    items = {item['key']: item for item in items}
    discovered = set(discoveries)
    unowned = set(unowned)
    assert discovered <= items.keys() and unowned <= items.keys()
    assert not discovered & unowned
    genres = {item.get('genre') for item in items.values()}
    resolved = {label: store.genres.resolve(label) for label in genres}
    assert all(reason is None for _key, reason in resolved.values()), resolved
    first = store.conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM recordings').fetchone()[0]
    subjects = [(rid, item) for rid, item in enumerate(items.values(), first)]
    # Empty genre input also serves installed-v79 publication fixtures, before
    # the keyed-genre column existed. Do not add a genre dependency to it.
    genre_input = any(item.get('genre') is not None for item in items.values())
    columns = 'id,identity_key,title,artist,created_at' + (',genre_key' if genre_input else '')
    with atomic_write(store):
        store.conn.executemany('INSERT INTO recordings(' + columns + ') VALUES (' +
            ','.join('?' for _ in columns.split(',')) + ')',
            [(rid, item['key'], item['title'], item['artist'], item.get('created_at')) +
             ((resolved[item.get('genre')][0],) if genre_input else ())
             for rid, item in subjects])
        store.conn.executemany('INSERT INTO youtube_items(video_id,recording_id,title,artist,orig_title,orig_artist,'
            'album,mb_year,duration_s) VALUES (?,?,?,?,?,?,?,?,?)',
            [(item['video_id'], rid, item['title'], item['artist'], item['title'], item['artist'],
              item.get('album'), item.get('year'), item.get('duration_s')) for rid, item in subjects])
        store.conn.executemany('INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (?,?,?)',
                               [(rid, rid, item['video_id']) for rid, item in subjects])
        store.conn.executemany("INSERT INTO recording_aliases VALUES (?,?,'fixture')",
                               [(item['key'], rid) for rid, item in subjects])
        store.conn.executemany("INSERT INTO library_memberships VALUES (?,'fixture',NULL)",
                               [(rid,) for rid, item in subjects if item['key'] not in discovered | unowned])
        store.conn.executemany('INSERT INTO discovery_memberships(recording_id,entry_id,found_at) VALUES (?,?,?)',
                               [(rid, rid, now) for rid, item in subjects if item['key'] in discovered])


def seed_bundle_subjects(store, bundle, *, fresh=()):
    """Give every playable fixture row its actual library/discovery owner."""
    tracks = {}
    for mode, bucket in bundle.items():
        if mode.startswith('_'):
            continue
        for lane, items in bucket.items():
            for item in items:
                if item.get('key') and 'title' in item and 'artist' in item:
                    previous = tracks.get(item['key'])
                    tracks[item['key']] = (lane != 'fresh' or bool(previous and previous[0]), item)
    for item in fresh:
        tracks.setdefault(item['key'], (False, item))
    # The complete candidate input is one fixture batch. Individual catalog
    # operations keep their real SQL and guards without committing each field.
    with atomic_write(store):
        owned = store.rec_query.tracks_by_keys(tracks)
        discovered = store.discovery.discovered_tracks_by_keys(tracks)
        for key, (library, item) in tracks.items():
            if key in owned or key in discovered:
                continue
            video = item.get('video_id') or 'fixture-' + key
            existing = store.conn.execute('SELECT r.identity_key FROM youtube_items y JOIN recordings r '
                                          'ON r.id=y.recording_id WHERE y.video_id=?', (video,)).fetchone()
            if existing and existing[0] != key:
                video = 'fixture-' + key
            if library:
                seed_catalog_track(store, key, item['title'], item['artist'], video_id=video,
                                   album=item.get('album'), genre=item.get('genre'))
            else:
                store.discovery.upsert_discovered_track(key, video, item['title'], item['artist'],
                    item.get('album'), item.get('thumbnail'), item.get('genre'), item.get('year'), None, 1)


def publish_bundles(store, bundle, now=1000, *, fresh=(), vibes=False,
                    retain_vibe_readiness=True, genre_reserves=True, retain_payload=True):
    with store.read_snapshot() as snapshot:
        policies = measurement.PoolPolicies(snapshot.store, now)
        if vibes:
            from yt_playlist.rec import vibe_pools
            vibe_pools.prepare(snapshot.store, bundle, now, policies)
        if not retain_vibe_readiness:
            # Publish genuine prepared candidates without the optional summary
            # in one transition; callers still exercise real typed publication.
            bundle.get('_meta', {}).pop('vibe_readiness', None)
        prepared = {key: value if key.startswith('_') else {
            lane: policies.stamp(items, lane) for lane, items in value.items()}
            for key, value in bundle.items()}
        if '_vibes' in prepared:
            # Stamping creates new dictionaries for every candidate. Copy only
            # this container; cloning all profile trees first duplicates the
            # same completed input across every preset and genre reserve.
            selected = dict(prepared['_vibes'])
            selected['pools'] = {preset: {lane: policies.stamp(items, 'vibe:' + preset)
                for lane, items in lanes.items()} for preset, lanes in selected.get('pools', {}).items()}
            selected['genres'] = {preset: {lane: {genre: policies.stamp(items, 'vibe:' + preset)
                for genre, items in groups.items()} for lane, groups in lanes.items()}
                for preset, lanes in selected.get('genres', {}).items()} if genre_reserves else {}
            prepared['_vibes'] = selected
        generation = mode_surfaces._publish_typed_bundles(store, snapshot, prepared,
            policies.stamp(list(fresh), 'fresh'), policies, {}, now)
    assert generation is not None
    if retain_payload:
        retained = bundle
        if not genre_reserves and '_vibes' in bundle:
            retained = {**bundle, '_vibes': {**bundle['_vibes'], 'genres': {}}}
        store._fixture_bundle_payload = deepcopy(retained)
    return generation
