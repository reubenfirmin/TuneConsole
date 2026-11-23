"""Shared inputs for Home preparation and its independent consumers."""
from copy import deepcopy
from types import SimpleNamespace

import numpy as np

from tests.musical_support import prepare_profiles
from tests.group_support import publish_keyed
from tests.recommendation_support import publish_bundles, seed_library_favorites, seed_publication_subjects
from yt_playlist.util.matching import identity_key


def publication_inputs(store, monkeypatch, *, tracks_per_card=1, stable_options=False, legacy=False):
    """Controlled menu calculation beneath real preparation and publication.

    Selection/ranking and typed pool heads remain owned by test_home_cards,
    test_stored_home and test_temporal_release_bands. These small real subjects
    let snapshot, retry and publication tests exercise their own boundary.
    """
    from yt_playlist.rec import home_cards

    genres = ('Jazz', 'Techno', 'Classical', 'Ambient', 'Trance', 'Indie Rock')
    lanes = ('wheelhouse', 'explore', 'comfort', 'fresh')
    bundles = {'all': {}, '_meta': {'full_pools': True}}
    for mid, genre in enumerate(genres):
        bundles[str(mid)] = {}
        for lane in lanes:
            source_lane, subject = ('fresh', 0) if lane == 'fresh' else ('wheelhouse', mid)
            tracks = [{'key': f'{source_lane} {subject} {index}|artist {subject} {index}',
                       'title': f'{source_lane} {subject} {index}', 'artist': f'Artist {subject} {index}',
                       'video_id': f'v-{source_lane}-{subject}-{index}', 'genre': genre, 'album': '',
                       'thumbnail': None, 'plays': 0, 'reason': '', 'lane': lane}
                      for index in range(tracks_per_card)]
            bundles[str(mid)][lane] = tracks
            bundles['all'].setdefault(lane, []).extend(tracks)
    # Metadata ingestion is not part of a publication precondition. Keep real
    # catalog identities and memberships without re-running every ingestion
    # observer, artist resolver and profile producer for each controlled track.
    library = list({track['key']: track for lane, tracks in bundles['all'].items()
                    if lane != 'fresh' for track in tracks}.values())
    fresh = {track['key']: track for track in bundles['all']['fresh']}
    seed_publication_subjects(store, [*library, *fresh.values()], discoveries=fresh, now=1)
    seed_library_favorites(store, bundles['all']['comfort'])
    styles = {}
    context = None
    if legacy:
        store.modes.replace_modes([
            {'mode_id': mid, 'label': genre, 'families': [], 'size': tracks_per_card,
             'centroid': np.eye(len(genres))[mid], 'rep_keys': []}
            for mid, genre in enumerate(genres)], [], 1)
        styles = {mid: {'mode_id': mid} for mid in range(len(genres))}
    else:
        group_keys = [[track['key'] for track in bundles[str(mid)]['wheelhouse']] for mid in range(len(genres))]
        membership = publish_keyed(store, [(genre, keys, mid / len(genres))
                                            for mid, (genre, keys) in enumerate(zip(genres, group_keys, strict=True))])
        generation = store.taste_groups.head_generation()
        definitions = {group['version']: group for group in store.taste_groups.definitions(generation)}
        for mid, keys in enumerate(group_keys):
            version = membership[keys[0]]
            styles[mid] = {'mode_id': None, 'group_id': definitions[version]['group_id'], 'group_version': version,
                           'group_generation': generation}
        context = {'group_generation': generation}
    publications = type(store.recommendations)
    head = publications.head
    monkeypatch.setattr(publications, 'head',
        lambda self, surface: {'id': 1} if surface == 'home' else head(self, surface))
    requests, options = [], None

    def cards(self, request):
        nonlocal options
        requests.append(request)
        if not stable_options or options is None:
            options = self.options()
        protos = []
        for index, lane in enumerate(lanes):
            mid = (request.mode_order[index] if request.mode_order else (request.epoch + index) % len(genres))
            proto = {'lane': lane, **styles[mid], 'label': f'{genres[mid]} {lane}',
                     'name': f'{genres[mid]} mix', 'note': 'Controlled producer output',
                     'genres': [genres[mid]], 'recipe': {},
                     'tracks': deepcopy(bundles[str(mid)][lane])}
            protos.append(proto)
        return home_cards.CardsResult(protos, [{'proto': p, 'pool': None} for p in protos],
            None, None, deepcopy(options), [], request.epoch, None, choice_context=context)

    monkeypatch.setattr(home_cards.HomeCards, 'cards', cards)
    return SimpleNamespace(store=store, requests=requests, cards=cards,
                           groups={style['group_id'] for style in styles.values() if 'group_id' in style})


def seed(store, *, tracks_per_lane=8, publish=True):
    modes, bundles = [], {"all": {}, "_meta": {"comfort_pool": 100, "full_pools": True}}
    for mid, genre in enumerate(("Jazz", "Techno", "Classical", "Ambient", "Trance", "Indie Rock")):
        modes.append({"mode_id": mid, "label": genre, "families": [], "size": 50,
                      "centroid": np.eye(6)[mid], "rep_keys": []})
        bundles[str(mid)] = {}
        for lane in ("wheelhouse", "explore", "comfort", "fresh"):
            items = []
            for i in range(tracks_per_lane):
                title, artist, video = f"{lane} {mid} {i}", f"Artist {mid} {i}", f"v-{lane}-{mid}-{i}"
                key = identity_key(title, artist)
                items.append({"key": key, "title": title, "artist": artist, "album": "", "video_id": video,
                              "thumbnail": None, "genre": genre, "plays": 0, "reason": "", "lane": lane})
            bundles[str(mid)][lane] = items
            bundles["all"].setdefault(lane, []).extend(items)
    # Home consumes completed catalog inputs. Provider ingestion, artist
    # inference and per-field delivery files have their own behavioral owners.
    # Preserve every subject, membership and genre before real preparation.
    seed_publication_subjects(store, [item for items in bundles['all'].values() for item in items],
                              discoveries=[item['key'] for item in bundles['all']['fresh']])
    seed_library_favorites(store, bundles['all']['comfort'])
    store.modes.replace_modes(modes, [], 1)
    prepare_profiles(store)
    if publish:
        publish_bundles(store, bundles, vibes=True)
    return bundles
