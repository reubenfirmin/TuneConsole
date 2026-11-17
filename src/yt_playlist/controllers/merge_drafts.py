"""Merge editor policy and durable draft/application commands."""
from copy import deepcopy
import json

from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.library.merge_order import track_positions


def liked_id(members):
    return next((m['id'] for m in members if m['ytm'] == 'LM'), None)


def effective_position(track, picks):
    pick, positions = picks.get(track['tid']), track['npos']
    if pick is not None and 0 <= pick < len(positions) and positions[pick] is not None:
        return positions[pick]
    values = [p for p in positions if p is not None]
    return sum(values) / len(values) if values else 1.


def ordered(tracks, params):
    title = lambda t: (t['title'] or '').lower()
    rows = sorted(tracks, key=lambda t: (effective_position(t, params['pick']), title(t))) if params['sort'] == 'playlist' else sorted(tracks, key=title)
    return sorted(rows, key=lambda t: 0 if sum(t['present']) >= 2 else 1) if params['mode'] == 'ducks' else rows


def edit(params, members, tracks, field, value):
    params = deepcopy(params)
    valid = {t['tid'] for t in tracks}
    if field == 'toggle' and value in valid:
        params['excluded'] = list(set(params['excluded']) ^ {value})
    elif field == 'setall':
        params['excluded'] = [] if value == '1' else list(valid)
    elif field == 'sort' and value in ('alpha', 'playlist'):
        params['sort'] = value
    elif field == 'mode' and value in ('interleaved', 'ducks'):
        params['mode'] = value
    elif field == 'keep' and value in {'all', *(str(m['id']) for m in members)}:
        liked = liked_id(members)
        if liked is None or value in (str(liked), 'all'):
            params['keep'] = value
    elif field == 'pick' and ':' in value:
        tid, _, index = value.rpartition(':')
        if tid in valid and index.isdigit() and int(index) < len(members):
            if params['pick'].get(tid) == int(index):
                params['pick'].pop(tid, None)
            else:
                params['pick'][tid] = int(index)
    return params


def member_tracks(playlists, labels, sequences):
    if len(playlists) < 2:
        return None
    members = [dict(id=p.id, letter=chr(65 + n), ident=labels.get(p.account_id, '?'), title=p.title,
                    ytm=p.provider_playlist_id) for n, p in enumerate(playlists)]
    tracks, orders = {}, [[] for _ in playlists]
    for member, rows in enumerate(sequences):
        for position, (key, video, title, artist, duration, available) in enumerate(rows):
            tid = 'v:' + video if video else 'k:' + key
            orders[member].append(tid)
            row = tracks.setdefault(tid, dict(tid=tid, title=title, artist=artist, video_id=video,
                available=available, duration=duration, present=[False] * len(playlists), pos=[None] * len(playlists)))
            if row['duration'] is None and duration is not None:
                row['duration'] = duration
            row['present'][member], row['pos'][member] = True, position + 1
    positions = track_positions(orders)
    lengths = [len(order) for order in orders]
    for row in tracks.values():
        row['order'] = positions.get(row['tid'], 1.)
        row['npos'] = [((row['pos'][i] - 1) / (lengths[i] - 1) if lengths[i] > 1 else 0.)
                       if row['pos'][i] is not None else None for i in range(len(playlists))]
    return members, sorted(tracks.values(), key=lambda row: (row['order'], (row['title'] or '').lower()))


class MergeDrafts:
    def __init__(self, playlists, accounts, actions, *, apply, now):
        self.playlists, self.accounts, self.actions = playlists, accounts, actions
        self.apply_merge, self.now = apply, now

    def members_tracks(self, ids):
        playlists = [p for pid in ids if (p := self.playlists.get_playlist(pid)) is not None]
        return member_tracks(playlists, {a.id: a.label for a in self.accounts.get_accounts()},
                             [self.playlists.get_playlist_tracks_with_meta(p.id) for p in playlists])

    def available(self, ids):
        return all(self.playlists.get_playlist(pid) is not None for pid in ids)

    def draft(self, ids, members, *, return_to=None):
        signature = ','.join(str(pid) for pid in ids)
        draft = self.actions.get_draft(signature)
        if draft is None:
            liked = liked_id(members)
            params = dict(excluded=[], pick={}, sort='playlist', mode='interleaved',
                          keep=str(liked if liked is not None else members[0]['id']), return_to=return_to or '/cleanup')
            draft_id = self.actions.record_action('merge_draft', json.dumps(params), signature, 'draft', None, self.now())
            draft = self.actions.get_action(draft_id)
        elif return_to:
            params = json.loads(draft.params_json)
            params['return_to'] = return_to
            self.actions.update_draft(draft.id, json.dumps(params))
            draft = self.actions.get_action(draft.id)
        return draft

    def update(self, draft, members, tracks, field, value):
        params = edit(json.loads(draft.params_json), members, tracks, field, value)
        self.actions.update_draft(draft.id, json.dumps(params))
        return self.actions.get_action(draft.id)

    def apply(self, ids):
        contents = self.members_tracks(ids)
        if contents is None:
            raise ActionInputError('merge_unavailable')
        members, tracks = contents
        draft = self.draft(ids, members)
        params = json.loads(draft.params_json)
        videos = [t['video_id'] for t in ordered(tracks, params) if t['tid'] not in params['excluded'] and t['video_id']]
        receipt = self.apply_merge([m['id'] for m in members], videos, params['keep'],
                                   reviewed_draft=(draft.id, draft.revision))
        self.actions.delete_action(draft.id)
        return receipt, params
