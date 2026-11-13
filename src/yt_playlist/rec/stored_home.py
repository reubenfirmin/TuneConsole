"""Home card selection over complete typed pools with bounded Python memory.

The private scratch database holds ordering keys, never a copied bundle or
catalog document. SQLite can spill sorts to disk; only rank pages and the
selected card are hydrated. No global cutoff can hide a rare family's tail.
"""
from collections import Counter
from contextlib import contextmanager
import hashlib
from itertools import permutations
import json

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import (experiments, group_roots, home_themes, layers, mode_eval,
                             negative, rec_params, transient)
from yt_playlist.rec.eligibility import home_exclusions
from yt_playlist.controllers.home_selection import HomeSelection
from yt_playlist.rec.home_selection_policy import CandidateFilter
from yt_playlist.repos.home_selection import HomeInputRepo, HomeSelectionRepo

LANES = ('wheelhouse', 'explore', 'fresh', 'comfort', 'temporal')


@contextmanager
def preparation_inputs():
    """Reuse per-recording checks within one batch without retaining a library in Python."""
    with HomeInputRepo.open() as inputs:
        yield PreparationInputs(inputs)


class PreparationInputs:
    def __init__(self, inputs):
        self.inputs, self.reader, self.now = inputs, None, None

    def resolve(self, reader, tracks, now):
        if self.reader is None:
            self.reader, self.now = reader, now
        if reader is not self.reader or now != self.now:
            raise ValueError('Home inputs require the same pinned reader and time')
        keys = [track['key'] for track in tracks]
        if not keys:
            return [], set()
        cached = self.inputs.get(keys)
        missing = [track for track in tracks if track['key'] not in cached]
        if missing:
            resolved = home_themes.resolved_genres(reader, missing)
            blocked = home_exclusions(reader, [track['key'] for track in missing], now)
            rows = [(track['key'], track.get('genre'), track['key'] in blocked) for track in resolved]
            self.inputs.add(rows)
            cached.update({key: (genre, blocked) for key, genre, blocked in rows})
        return ([{**track, 'genre': cached[track['key']][0]} for track in tracks],
                {key for key, (_, blocked) in cached.items() if blocked})


class Selection:
    def __init__(self, reader, generation, now, leans, ordering, inputs=None):
        self.reader, self.generation, self.now, self.leans = reader, generation, now, leans
        self.inputs = inputs
        self.ordering = ordering
        self.slots, self.loaded, self.owners, self.ppr_counts = {}, set(), {}, {}
        for lane in LANES:
            for slot in reader.recommendations.selections('home', lane, generation=generation):
                self.slots[slot['mode_key'], lane] = slot['id']
        self.muted = reader.rec_model.muted_artists()
        self.steering = negative.Steering(reader)

    def load(self, mode, lane):
        pool = self.slots.get((str(mode), lane))
        if pool is None or pool in self.loaded:
            return pool
        self.owners[pool] = str(mode), lane
        after = -1
        while True:
            check_cancellable_work()
            page = self.reader.recommendations.candidate_page('home', str(mode), lane,
                generation=self.generation, after=after, limit=128)
            if self.inputs is not None:
                tracks, blocked = self.inputs.resolve(self.reader, page['tracks'], self.now)
            else:
                tracks = home_themes.resolved_genres(self.reader, page['tracks'])
                blocked = home_exclusions(self.reader, [track['key'] for track in tracks], self.now)
            keys = [track['key'] for track in tracks]
            deltas = self.steering.for_keys(keys)
            # Every row belongs to this pinned reader. Resolve exact labels once
            # per bounded page; a later request still sees current taxonomy.
            axes = self.reader.genres.feature_axes({track.get('genre') for track in tracks})
            self.ordering.add_candidates([
                (pool, track['pool_rank'], track['key'], track.get('artist') or '', track.get('album') or '',
                 axes.get(track.get('genre'), ('', None))[0], bool(track.get('genre')), track.get('year'),
                 1.0 + self.leans.get('genre:' + (track.get('genre') or ''), 0.0),
                 deltas.get(track['key'], 0.0), track['key'] not in blocked and track.get('artist') not in self.muted)
                for track in tracks])
            after = page['after']
            if page['exhausted']:
                break
        self.loaded.add(pool)
        return pool

    def load_ppr(self, mode):
        mode = str(mode)
        if mode in self.ppr_counts:
            return self.ppr_counts[mode]
        pool = self.reader.recommendations.pool('radio', mode, 'ppr', generation=self.generation)
        self.ppr_counts[mode] = pool['eligible_count'] if pool else 0
        after = -1
        while pool:
            check_cancellable_work()
            rows = self.reader.recommendations.page('radio', mode, 'ppr', generation=self.generation,
                                                     after=after, limit=128)
            self.ordering.add_ppr([(mode, row['key'], row['rank']) for row in rows])
            if len(rows) < 128:
                break
            after = rows[-1]['rank']
        return self.ppr_counts[mode]

    @staticmethod
    def diverse(rows, artist_cap, album_cap, initial=()):
        selected = list(initial)
        artists = Counter(row['artist'] for row in selected)
        albums = Counter(row['album'] for row in selected if row['album'])
        if len(selected) >= 12:
            return selected
        for row in rows:
            if artists[row['artist']] >= artist_cap or row['album'] and albums[row['album']] >= album_cap:
                continue
            selected.append(row)
            artists[row['artist']] += 1
            if row['album']:
                albums[row['album']] += 1
            if len(selected) == 12:
                break
        return selected

    def select(self, mode, ranker, selected, caps, *, family=None, initial=()):
        scale = (max(1, self.ppr_counts[str(mode)]) if
                 self.ordering.has_negative_ordering() else 1)
        rows = self.ordering.ranked(selected, mode=mode, ranker=ranker, family=family, scale=scale)
        return self.diverse(rows, *caps, initial=initial)

    def hydrate(self, rows, lane, ranker, mode):
        selections = {}
        for row in rows:
            owner = self.owners[row['pool']]
            selections.setdefault(owner, []).append(row['key'])
        # A card holds at most twelve rows. Resolve their current catalog and
        # display fields together, then restore selection order (the repository
        # returns each pool's rows in rank order).
        pages = self.reader.recommendations.selected_candidates('home',
            [(owner, purpose, keys) for (owner, purpose), keys in selections.items()],
            generation=self.generation)
        candidates = {owner: {track['key']: track for track in tracks}
                      for owner, tracks in zip(selections, pages, strict=True)}
        tracks = []
        for row in rows:
            track = candidates[self.owners[row['pool']]][row['key']]
            position = self.ordering.ppr_position(mode, row['key'])
            tracks.append({**track, 'serving_context': {**track['serving_context'],
                'score_kind': 'candidate_fit', 'source_ranker': lane, 'ordering_ranker': ranker,
                'ppr_position': position if ranker == 'ppr' else None,
                'facet_tilt': row['tilt'], 'vibe': '', 'negative_delta': row['delta'], 'vibe_affinity': .5}})
        return tracks

    def experiment(self, card, policy, share, caps, leans, epoch, mode):
        """Hash the exact complete opportunity without constructing its key list."""
        fingerprint = hashlib.sha256(b'[')
        cohort = hashlib.sha256()
        # Match the established json.dumps(sort_keys=True) evidence encoding.
        cohort.update(('{"caps": ' + json.dumps(list(caps)) + ', "leans": ' +
                       json.dumps(leans, sort_keys=True) + ', "negative": {').encode())
        count, negatives = 0, 0
        for row in self.ordering.considered_rows(card):
            fingerprint.update(((', ' if count else '') + json.dumps(row['key'])).encode())
            count += 1
            if row['delta']:
                cohort.update(((', ' if negatives else '') + json.dumps(row['key']) + ': ' +
                               json.dumps(row['delta'])).encode())
                negatives += 1
        fingerprint.update(b']')
        cohort_policy = {key: value for key, value in policy.items() if key != 'built_at'}
        cohort.update(('}, "policy": ' + json.dumps(cohort_policy, sort_keys=True) +
                       ', "share": ' + json.dumps(share) + ', "vibe": ""}').encode())
        return {'schema': 1, 'name': 'ppr-cosine-v1', 'cohort': cohort.hexdigest()[:16],
                'cluster': f'{epoch}:{mode}', 'share': share,
                'state': 'shadow' if share == 0 else 'retained' if share == 1 else 'live',
                'candidate_fingerprint': fingerprint.hexdigest(), 'candidate_count': count, 'ppr_policy': policy}


def assemble(store, now, epoch, *, avoid_genres=(), include_genres=(), mode_order=None, preparation=None):
    if not getattr(store, '_read_only', False):
        # Pin the publication and its files together. Browsing does not publish
        # evidence and must not wait for the evidence recovery admission lock.
        with store.read_view() as reader:
            return assemble(reader, now, epoch, avoid_genres=avoid_genres, include_genres=include_genres,
                            mode_order=mode_order, preparation=preparation)
    generation, close = group_roots.pending_head(store, ('home',), now)
    try:
        if generation is None:
            return []
        if getattr(store, '_read_only', False):
            return _assemble(store, generation, now, epoch, avoid_genres, include_genres, mode_order, preparation)
        with store.read_view() as reader:
            return _assemble(reader, generation, now, epoch, avoid_genres, include_genres, mode_order, preparation)
    finally:
        if close:
            close()


def unmodeled_cards(store, now, *, card_builder, include=(), avoid=(), preparation=None):
    """Offer bounded cached library selections while the first modes are pending."""
    if not getattr(store, '_read_only', False):
        with store.read_view() as reader:
            return unmodeled_cards(reader, now, card_builder=card_builder,
                                   include=include, avoid=avoid, preparation=preparation)
    generation, close = group_roots.pending_head(store, ('home',), now)
    try:
        if generation is None:
            return []
        if not getattr(store, '_read_only', False):
            with store.read_view() as reader:
                return _unmodeled_cards(reader, generation, now, include, avoid, preparation, card_builder)
        return _unmodeled_cards(store, generation, now, include, avoid, preparation, card_builder)
    finally:
        if close:
            close()


def _unmodeled_cards(reader, generation, now, include, avoid, preparation, card_builder):
    from yt_playlist.rec.mode_selection import CARD_LABELS, ROTATION_POOL
    framing = reader.recommendations.framing('home', generation=generation) or {}
    if not framing.get('unmodeled') and reader.modes.list_modes(active_only=True):
        return []
    muted, protos = reader.rec_model.muted_artists(), []
    for lane in ('wheelhouse', 'explore', 'comfort'):
        used = {track['key'] for proto in protos for track in proto['tracks']} if lane == 'explore' else set()
        tracks, after = [], -1
        while len(tracks) < ROTATION_POOL:
            check_cancellable_work()
            page = reader.recommendations.candidate_page('home', 'all', lane,
                generation=generation, after=after, limit=128)
            eligible = home_themes.matching_genres(reader, page['tracks'], include=include, avoid=avoid)
            blocked = home_exclusions(reader, [track['key'] for track in eligible], now)
            tracks.extend(track for track in eligible if track['key'] not in blocked
                          and track['key'] not in used and track.get('artist') not in muted)
            del tracks[ROTATION_POOL:]
            after = page['after']
            if page['exhausted']:
                break
        if len(tracks) >= 4:
            proto = card_builder(reader, lane, CARD_LABELS[lane], tracks, now, preparation)
            if len(proto['tracks']) >= 4:
                protos.append(proto)
    return protos


def _assemble(reader, generation, now, epoch, avoid, include, mode_order, preparation):
    framing = reader.recommendations.framing('home', generation=generation) or {}
    if framing.get('unmodeled'):
        return []
    modes = reader.modes.list_modes(active_only=True)
    if not modes:
        return []
    leans = preparation['leans'] if preparation is not None else transient.facet_leans(reader, now)
    with HomeSelectionRepo.open() as repository:
        ordering = HomeSelection(repository)
        selection = Selection(reader, generation, now, leans, ordering,
                              (preparation or {}).get('metadata_inputs'))
        return _cards(selection, modes, framing, leans, epoch, avoid, include, mode_order)


def _cards(selection, modes, framing, leans, epoch, avoid, include, mode_order):
    from yt_playlist.rec import mode_selection
    reader, now, ordering = selection.reader, selection.now, selection.ordering
    caps = (int(rec_params.get_param(reader, 'modes_artist_cap')), int(rec_params.get_param(reader, 'modes_album_cap')))
    band = epoch % 3
    years = (framing.get('year_low'), framing.get('year_high'))
    has_years = all(year is not None for year in years)
    comfort = (framing.get('comfort_pool') or 0) >= int(rec_params.get_param(reader, 'comfort_min_pool'))
    fourth = 'comfort' if comfort else 'temporal' if has_years else None
    active = list(LANES[:3]) + ([fourth] if fourth else [])

    def predicate(mode, lane, *, eligible=False, unseen=False, date_band=True):
        return CandidateFilter(selection.load(mode, lane),
            year_band=band if date_band and lane == 'temporal' and has_years else None,
            years=years, include=tuple(include), avoid=tuple(avoid), tagged=bool(avoid or include),
            eligible=eligible, unseen=unseen)

    if avoid or include:
        filtered = []
        for mode in modes:
            family = max(mode['families'], key=lambda value: value[1])[0] if mode.get('families') else None
            if family in avoid or include and family and family not in include:
                continue
            if any(ordering.count(predicate(mode['mode_id'], lane, date_band=False)) >= 4 for lane in LANES):
                filtered.append(mode)
        modes = filtered
    n = max(4, int(rec_params.get_param(reader, 'modes_menu_size')))
    if mode_order is None:
        evidence_before = reader.modes.epoch_started_at(epoch)
        now_posterior = layers.now_mode_posterior(reader, now)
        chosen = mode_selection.select_modes(modes, leans, epoch, n=n,
            stats=mode_eval.mode_bandit_stats(reader, before=evidence_before, now=now),
            now_posterior=now_posterior,
            now_gain=rec_params.get_param(reader, 'now_gain') if now_posterior else None)
    else:
        available = {mode['mode_id'] for mode in modes}
        chosen = [mode for mode in mode_order if mode in available][:n]
    if not chosen:
        return []
    depths = {}
    for lane in active:
        for mode in chosen:
            selected = predicate(mode, lane, eligible=True)
            depths[lane, mode] = len(selection.diverse(ordering.ranked(selected), *caps))
    priority = {mode: index for index, mode in enumerate(chosen)}
    def score(modes):
        sizes = [depths.get((lane, mode), 0) for lane, mode in zip(active, modes)]
        return sum(size >= 4 for size in sizes), sum(sizes), tuple(-priority.get(mode, len(chosen)) for mode in modes)
    best = max(permutations(chosen + [None] * max(0, len(active) - len(chosen)), len(active)), key=score)
    families = {mode['mode_id']: max(mode['families'], key=lambda value: value[1])[0]
                for mode in modes if mode.get('families')}
    share = float(rec_params.get_param(reader, 'ppr_ab_share'))
    cards, opportunity = [], 0

    def card_for(lane, mode, ranker, card):
        requested = predicate(mode, lane, eligible=True, unseen=True)
        ordering.considered(card, requested)
        selected = selection.select(mode, ranker, requested, caps)
        if lane != 'comfort' and len(selected) < 12:
            # Keep explicit genre choices when filling a thin mode bucket.
            # Nearby families may widen an unconstrained menu only.
            family = families.get(mode)
            extra = CandidateFilter(selection.load('all', lane), eligible=True, unseen=True,
                excluded=tuple(row['key'] for row in selected), avoid=tuple(avoid),
                include=tuple(include), near_family=family, tagged=bool(family or include),
                year_band=band if lane == 'temporal' and has_years else None, years=years)
            ordering.considered(card, extra)
            selected = selection.select(mode, ranker, extra, caps, family=family, initial=selected)
        if len(selected) < 4:
            return None
        label = mode_selection.BAND_LABELS[band] if lane == 'temporal' else mode_selection.CARD_LABELS[lane]
        return {'lane': lane, 'label': label, 'mode_id': mode, 'ranker': ranker,
                'tracks': selection.hydrate(selected, lane, ranker, mode)}

    def attempt(lane, mode):
        nonlocal opportunity
        opportunity += 1
        ppr = selection.load_ppr(mode)
        ranker = mode_selection.ranker_for(epoch, mode, share) if ppr else 'cosine'
        card = card_for(lane, mode, ranker, opportunity)
        if card is None:
            return False
        if ppr and not (share == 0 and experiments.last_decision(reader).get('decision') == 'retire'):
            other = 'cosine' if ranker == 'ppr' else 'ppr'
            replay = card_for(lane, mode, other, opportunity)
            if replay:
                policy = (reader.recommendations.output('home', 'ranking-evidence', generation=selection.generation) or
                          {}).get('ppr_policy', {'algorithm': 'unknown-legacy-cache'})
                card['experiment'] = selection.experiment(opportunity, policy, share, caps, leans, epoch, mode)
                card['counterfactual'] = {'ranker': other, 'tracks': replay['tracks']}
        ordering.mark_seen(predicate(mode, lane), [track['key'] for track in card['tracks']])
        cards.append(card)
        return True

    for lane, mode in zip(active, best):
        if mode is None or attempt(lane, mode) or lane != 'comfort' or not has_years:
            continue
        used = {card['mode_id'] for card in cards}
        available = [mode for mode in chosen if mode not in used]
        def count(mode):
            return ordering.count(predicate(mode, 'temporal'))
        for fallback in sorted(available, key=lambda mode: -count(mode)):
            if attempt('temporal', fallback):
                break
    return cards
