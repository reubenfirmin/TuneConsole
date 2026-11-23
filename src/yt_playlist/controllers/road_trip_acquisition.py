"""Bounded passenger/provider acquisition; publish is owned by RoadTripDrafts."""
from yt_playlist.rec import road_trip_rules as policy, road_trip_axes as axes
from yt_playlist.rec.road_trip_candidates import InputRequest, rank_rows


class RoadTripAcquisition:
    def __init__(self, transitions, *, fetch, project, durations, facts, context, songs, feature_axes):
        self.transitions = transitions
        self.fetch, self.project, self.durations, self.facts = fetch, project, durations, facts
        self.context, self.songs, self.feature_axes = context, songs, feature_axes

    def add(self, state, client, item, now=0.):
        name, kind = item['name'], item['kind']
        input_key = policy.input_key(item)
        want = item.get('want') or ''
        decade = want.split(':', 1)[1] if want.startswith('era:') else item.get('decade')
        neighborhoods = policy._neighborhoods(state)
        fresh, related = None, []
        if kind == 'genre':
            genre = want.split(':', 1)[1] if want.startswith('genre:') else name
            if decade:
                genre = genre.removesuffix(f' {decade}s')
            batch = neighborhoods.resolve(state['pool'], genre, state['other_cap'],
                context=lambda name, **kwargs: self.context(client, name, **kwargs), project=self.project,
                feature_axes=self.feature_axes, songs=lambda name, **kw: self.songs(client, name, **kw))
            if batch is not None:
                state['pool'], fresh = batch.pool, batch.fresh
        known = {c['video_id'] for c in state['pool']}
        theirs = [c for c in state['pool'] if c['source'] != 'mine']
        room = max(0, state['other_limit'] - len(theirs))
        if fresh is None:
            batch = self.fetch(client, InputRequest(kind, name, state['other_cap'], decade, item.get('browse_id')))
            rows, related = batch.rows, batch.related
            rows = [{**row, 'input': input_key} for row in rows if row['video_id'] not in known][:room]
            fresh = self.project(rows)
            if kind == 'genre':
                origin = neighborhoods.genre_input(state['pool'], genre)
                for candidate in fresh:
                    candidate['genre_input'] = origin or genre
        else:
            fresh = fresh[:room]
        fresh = neighborhoods.attribute_input(input_key, kind, fresh, theirs, related, item.get('anchor'))
        if want:
            fresh = [c for c in fresh if axes.matches(c, want)]
        self.durations(client, fresh, cap=2)
        combined = theirs + fresh
        popularity = {c['video_id']: self.facts(c['title'], c['artist']).get('popularity') for c in combined}
        state['pool'] = [c for c in state['pool'] if c['source'] == 'mine'] + rank_rows(combined, popularity)
        state['done_inputs'].append(input_key if item.get('artist_key') else item.get('browse_id') or name)
        if item.get('widen_key'):
            state.setdefault('done_widening', []).append(item['widen_key'])
        if related and not item.get('related') and len(combined) < state['other_limit']:
            queued = {p.get('browse_id') or p['name'] for p in state['pending']} | set(state['done_inputs'])
            explicit = {neighborhoods.refs.get(key, {}).get('browse_id') or key
                        for key in neighborhoods.artists}
            for ref in related:
                key = ref['browse_id'] or ref['name']
                if key not in explicit and ref['name'] not in explicit and key not in queued:
                    state['pending'].append({'kind': 'artist', 'name': ref['name'], 'browse_id': ref['browse_id'],
                                             'related': True, 'anchor': input_key})
                    queued.add(key)
        return self.transitions.repick(state, now)

    def fill(self, state, now=0, count=policy.OWN_FACT_LOOKUPS):
        with self.transitions.inputs() as facts:
            own = facts.own(state, now)
        bare = sorted((c for c in own if not (c['genre'] and c['year'])), key=lambda c: -c['engagement'])
        learned = state.setdefault('own_facts', {})
        for candidate in bare[:count]:
            facts = self.facts(candidate['title'], candidate['artist'])
            got = {'genre': candidate['genre'] or facts.get('genre') or '',
                   'year': candidate['year'] or facts.get('year')}
            if got['genre'] or got['year']:
                learned[candidate['video_id']] = got
        state['own_facts_done'] = state.get('own_facts_done', 0) + len(bare[:count])
        state['own_facts_left'] = max(0, len(bare) - count)
        return self.transitions.repick(state, now)
