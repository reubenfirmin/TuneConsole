"""Story query, provider work and guarded publication over explicit application ports."""
import json

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.providers.base import LookupUnavailable
from yt_playlist.rec import story_rules as rules
from yt_playlist.util.matching import is_remix


class StoryPreparation:
    def __init__(self, *, charts, genres, wiki, proposals, ledger, leans, snapshot=None, fetch=None,
                 taxonomy=rules.EMPTY_TAXONOMY):
        self.charts, self.genres, self.wiki = charts, genres, wiki
        self.rec_proposals, self.rec_ledger, self.leans = proposals, ledger, leans
        self.snapshot, self.fetch = snapshot, fetch
        self.taxonomy = taxonomy

    def baseline(self):
        corpus = self.charts.corpus_distribution('genre')
        return rules._baseline_genre_shares(corpus, self.genres.feature_axes(corpus))

    def groups(self, cards):
        return rules.story_groups(cards, taxonomy=self.taxonomy)

    def ranked(self, now):
        leans = self.leans(now)
        activity = self.charts.story_activity(now - rules.RECENT_WINDOW_S)
        return rules.ranked_subjects(leans=leans, activity=activity,
            favorites=self.charts.top_artists(limit=rules.FAVORITE_TOP_N), genre_base=self.baseline(),
            axes=self.genres.feature_axes(activity['genre']), taxonomy=self.taxonomy)

    def familiar(self, now):
        return rules._familiar_genres(self.charts.listen_distribution('genre',
            until=now - rules.FAMILIAR_GENRE_AGE_S), taxonomy=self.taxonomy)

    def connected(self, subjects, familiar=frozenset(), now=None):
        subjects = subjects[:rules.MAX_CANDIDATES * 3]
        rows = {s['subject']: rules._matching_summary(s, self.wiki.get(s['subject']), taxonomy=self.taxonomy)
                for s in subjects}
        misses = {key for key, row in rows.items() if now is not None and row and not row['found']
                  and self.wiki.is_fresh(row, now)}
        return rules._connected_subjects(subjects, rows=rows,
            activity=self.charts.story_activity((now or 0) - rules.RECENT_WINDOW_S),
            familiar=familiar, fresh_misses=misses, taxonomy=self.taxonomy)

    def fetch_plan(self, groups):
        rows = {s['subject']: rules._matching_summary(s, self.wiki.get(s['subject']), taxonomy=self.taxonomy)
                for group in groups for s in group}
        return rules._fetch_plan(groups, rows)

    def decorate(self, subject):
        if subject['kind'] == 'artist':
            return rules.decorate(subject, thumbnail=self.charts.artist_thumbnail(subject['display']),
                                  taxonomy=self.taxonomy)
        token = subject['subject'].split(':', 1)[1]
        return rules.decorate(subject, representative=self.charts.genre_representative(self.genres_in_token(token)),
                              taxonomy=self.taxonomy)

    def genres_in_token(self, token):
        return rules._genres_in_token(self.charts.corpus_distribution('genre'), token, taxonomy=self.taxonomy)

    def songs(self, card, group, now):
        if card['kind'] == 'artist':
            return self.charts.story_tracks(since=now - rules.SONG_WINDOW_S, artist=card['display'])
        if card['genre'] not in rules._UMBRELLA_GENRES:
            return self.charts.story_tracks(since=now - rules.SONG_WINDOW_S, genres=self.genres_in_token(card['genre']))
        songs = {}
        for artist in group:
            if artist['kind'] == 'artist':
                for song in self.charts.story_tracks(since=now - rules.RECENT_WINDOW_S, artist=artist['display']):
                    if (song['recent_plays'] and not is_remix(song['title'])
                            and rules._genre_key(song['genre'], taxonomy=self.taxonomy) == card['genre']):
                        songs.setdefault(song['key'], song)
        return list(songs.values())[:3]

    def refresh_summary(self, subject, now, fetch_fn=None):
        try:
            payload = self.fetch(subject, now, fetch_fn)
        except LookupUnavailable as exc:
            self.wiki.defer(subject['subject'], now, retry_at=getattr(exc, 'retry_at', 0))
            return
        self.wiki.put(subject['subject'], subject['kind'], subject['display'],
                      rules._matching_summary(subject, payload, taxonomy=self.taxonomy), now)

    def prewarm(self, now, fetch_fn=None, limit=rules.PREWARM_POOL, *, subjects=None, fetch_limit=None):
        fetched = 0
        pool = self.ranked(now) if subjects is None else subjects
        for subject in pool[:limit]:
            check_cancellable_work()
            row = rules._matching_summary(subject, self.wiki.get(subject['subject']), taxonomy=self.taxonomy)
            if not self.wiki.should_refresh(subject['subject'], row, now):
                continue
            self.refresh_summary(subject, now, fetch_fn)
            fetched += 1
            if fetch_limit is not None and fetched >= fetch_limit:
                break
        return fetched

    def publish_pool(self, now):
        with self.snapshot() as source:
            reader = source.inputs
            ranked = reader.ranked(now)
            previous = reader.rec_proposals.get_proposals(rules.SURFACE) or {}
            if not isinstance(previous, dict):
                previous = {}
            same = previous.get('schema') == rules.SCHEMA and 0 <= now - previous.get('edition_at', 0) < rules.EDITION_HOLD_S
            edition_at = previous['edition_at'] if same else now
            appearances = previous.get('appearances', {}) if same else reader.rec_ledger.story_views()
            familiar = reader.familiar(now)
            groups = reader.connected(rules._variety_order(ranked, appearances, now), familiar, now)
            subjects = reader.fetch_plan(groups)
            cards, seen = [], set()
            for group in groups:
                ready, pages = [], set()
                for subject in group:
                    check_cancellable_work()
                    row = rules._matching_summary(subject, reader.wiki.get(subject['subject']), taxonomy=reader.taxonomy)
                    if (not row or not row['found'] or not row['extract']
                            or not rules._usable_article({**row, **subject}, taxonomy=reader.taxonomy)):
                        continue
                    page = row['url'] or subject['subject']
                    if page in seen or page in pages:
                        continue
                    extras = reader.decorate(subject)
                    thumbnail = row['thumbnail'] or (extras['thumbnail'] if subject['kind'] == 'artist' else None)
                    if not thumbnail and subject['kind'] == 'artist':
                        continue
                    shown = appearances.get(subject['subject'], {}).get('last_shown')
                    ready.append({**row, 'thumbnail': thumbnail, 'color': extras['color'],
                        'group': group[0]['subject'], 'seed': extras['seed'], 'genre': extras['genre'],
                        'depth': rules.CLUSTER_DEPTH, 'recently_shown': shown is not None and now - shown < rules.STORY_COOLDOWN_S,
                        'lead_allowed': subject['kind'] == 'artist' or rules._genre_can_lead(
                            subject, row, familiar, taxonomy=reader.taxonomy)})
                    pages.add(page)
                for prepared in rules.story_groups(ready, taxonomy=reader.taxonomy):
                    if len(cards) + len(prepared) > rules.PREWARM_POOL:
                        continue
                    for card in prepared:
                        card['songs'] = reader.songs(card, prepared, now)
                        cards.append(card)
                        seen.add(card['url'] or card['subject'])
                if len(cards) == rules.PREWARM_POOL:
                    break
            pending = any(self.wiki.should_refresh(s['subject'], rules._matching_summary(
                s, reader.wiki.get(s['subject']), taxonomy=reader.taxonomy), now)
                          for s in subjects)
            payload = {'schema': rules.SCHEMA, 'built_at': now, 'edition_at': edition_at,
                'appearances': {s['subject']: appearances[s['subject']] for s in ranked if s['subject'] in appearances},
                'cards': cards, 'pending': pending}
            if (0 <= now - previous.get('built_at', 0) < rules.MAX_AGE_S
                    and {k: v for k, v in previous.items() if k != 'built_at'} == {k: v for k, v in payload.items() if k != 'built_at'}):
                payload['built_at'] = previous['built_at']
            encoded = json.dumps(payload, separators=(',', ':'))
            with self.rec_proposals.prepare_home(encoded, now, surface=rules.SURFACE) as publication:
                if not source.publish(lambda: self.rec_proposals.publish_home(
                        publication, input_revision=source.revision)):
                    return None
            return subjects, pending

    def prepare(self, now, fetch_fn=None):
        result = self.publish_pool(now)
        if result is None:
            return True
        subjects, pending = result
        if pending:
            self.prewarm(now, fetch_fn, subjects=subjects, limit=rules.MAX_CANDIDATES, fetch_limit=rules.MAX_SUMMARY_FETCHES)
            result = self.publish_pool(now)
        return result is None or result[1]

    def load(self, now, epoch=0, schedule=lambda: None):
        try:
            payload = self.rec_proposals.get_proposals(rules.SURFACE)
        except (TypeError, ValueError):
            payload = None
        cards, pending, refresh = rules.load(payload, now, epoch, taxonomy=self.taxonomy)
        if refresh:
            schedule()
        return cards, pending
