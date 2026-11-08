"""Shared inputs for Home preparation and its independent consumers."""
from copy import deepcopy
from types import SimpleNamespace

import numpy as np

from tests.musical_support import prepare_profiles
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from yt_playlist.util.matching import identity_key


def publication_inputs(store, monkeypatch, *, tracks_per_card=1):
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
    store.modes.replace_modes([
        {'mode_id': mid, 'label': genre, 'families': [], 'size': 4,
         'centroid': np.eye(6)[mid], 'rep_keys': []}
        for mid, genre in enumerate(genres)], [], 1)
    proposal_repo = type(store.rec_proposals)
    has_proposals = proposal_repo.has_proposals
    monkeypatch.setattr(proposal_repo, 'has_proposals',
        lambda self, surface: True if surface == 'mode_bundles' else has_proposals(self, surface))
    requests = []

    def cards(self, request):
        requests.append(request)
        order = request.mode_order or tuple(range(6))
        protos = []
        for index, lane in enumerate(lanes):
            mid = order[index % len(order)]
            proto = {'lane': lane, 'mode_id': mid, 'label': f'{genres[mid]} {lane}',
                     'name': f'{genres[mid]} mix', 'note': 'Controlled producer output',
                     'genres': [genres[mid]], 'recipe': {},
                     'tracks': deepcopy(bundles[str(mid)][lane])}
            protos.append(proto)
        return home_cards.CardsResult(protos, [{'proto': p, 'pool': None} for p in protos],
            None, None, self.options(), [], request.epoch, None)

    monkeypatch.setattr(home_cards.HomeCards, 'cards', cards)
    return SimpleNamespace(store=store, requests=requests, cards=cards)


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
                if lane == "fresh":
                    store.discovery.upsert_discovered_track(key, video, title, artist, "", None, genre,
                                                            None, None, 1)
                else:
                    tid = store.tracks.upsert_track(video, title, artist, "", None)
                    store.tracks.set_track_genre(tid, genre)
                items.append({"key": key, "title": title, "artist": artist, "album": "", "video_id": video,
                              "thumbnail": None, "genre": genre, "plays": 0, "reason": "", "lane": lane})
            bundles[str(mid)][lane] = items
            bundles["all"].setdefault(lane, []).extend(items)
    store.modes.replace_modes(modes, [], 1)
    prepare_profiles(store)
    if publish:
        publish_bundles(store, bundles, vibes=True)
    return bundles
