"""Detached membership, ownership, occurrence and collection decisions."""
from collections import Counter, defaultdict, deque
from types import SimpleNamespace


def copy_memberships(sources, destination=(), *, recipe=None, policy='recording'):
    """Plan from ordered (recording key, exact upload) facts; never substitute uploads.

    ``policy`` names the existing caller's deduplication contract. Version policy
    can be supplied here later without acquiring destination state in a planner.
    """
    if policy not in {'recording', 'upload'}:
        raise ValueError('unknown membership policy')
    have = {key if policy == 'recording' else video for key, video in destination}
    seen, result = set(), []
    for source in sources:
        for key, video in source:
            token = key if policy == 'recording' else video
            if video and video not in seen and token not in have:
                result.append((key, video))
                seen.add(video)
                have.add(token)
    return result


def generated_memberships(tracks, destination=(), *, recipe=None, policy='upload', maximum=None):
    """Keep the posted exact-upload order, using only supplied membership and length facts."""
    selected = copy_memberships([[(t.get('key'), t.get('video_id')) for t in tracks]],
                                destination, recipe=recipe, policy=policy)
    by_video = {}
    for track in tracks:
        by_video.setdefault(track.get('video_id'), track)
    return [by_video[video] for _, video in selected
            if maximum is None or by_video[video].get('duration') is None or by_video[video]['duration'] <= maximum]


def tv_continuation(items, destination, *, recipe, policy='upload'):
    present = {video for _, video in destination}
    old = [track for track in recipe.get('videos', []) if track['video_id'] in present]
    return old, generated_memberships(items, destination, recipe=recipe, policy=policy)


def collection_targets(playlists, eligible, retries, now):
    """Retain due durable intentions even after local rows have disappeared."""
    current = {(p.account_id, p.provider_playlist_id): p for p in playlists}
    targets = {key: p for key, p in current.items() if p.id in eligible}
    for account, remote, deadline in retries:
        key = account, remote
        if deadline > now:
            targets.pop(key, None)
        else:
            targets.setdefault(key, current.get(key) or SimpleNamespace(
                id=None, account_id=account, provider_playlist_id=remote, title=remote))
    return list(targets.values())


def placement_moves(tracks, plan):
    videos = plan['video_ids']
    order = [t['videoId'] for t in tracks if t.get('videoId') and t['videoId'] not in videos]
    anchor = plan['after_video_id']
    if anchor not in order:
        raise ValueError('the original track is not visible on YouTube yet')
    index = order.index(anchor) + 1
    if index == len(order):
        return []
    successor = order[index]
    handles = {t['videoId']: t['setVideoId'] for t in tracks if t.get('videoId') and t.get('setVideoId')}
    handles.update(plan.get('set_video_ids', {}))
    if any(not handles.get(video) for video in [successor, *videos]):
        raise ValueError('YouTube has not exposed the playlist item handles yet')
    return [(handles[video], handles[successor]) for video in videos]


def equivalent_siblings(kept, playlists, memberships, system_ids):
    groups = {}
    for playlist in playlists:
        if (playlist.playlist_scope_id != kept.playlist_scope_id
                and playlist.provider_playlist_id not in system_ids
                and memberships[playlist.id] == memberships[kept.id]):
            groups.setdefault(playlist.playlist_scope_id, []).append(playlist)
    return groups


def permission_fallback(status, action_status):
    # Uncertain outcomes never authorize a second account's destructive request.
    return status == 403 and action_status == 'delete_failed'


def unique_reconciliation(tracks, desired):
    desired = list(dict.fromkeys(v for v in desired if v))
    wanted, current = set(desired), {t['videoId'] for t in tracks}
    additions = [v for v in desired if v not in current]
    removals, kept = [], set()
    for track in tracks:
        video = track['videoId']
        if video not in wanted or video in kept:
            if video in wanted and video in kept and not track.get('setVideoId'):
                raise ValueError("YouTube has not exposed the repeated item's removal handle.")
            removals.append(track)
        kept.add(video)
    retained = list(dict.fromkeys(t['videoId'] for t in tracks if t['videoId'] in wanted))
    return additions, removals, retained


def missing_occurrences(tracks, desired):
    wanted, present = Counter(desired), Counter(t['videoId'] for t in tracks)
    remaining = wanted - present
    additions = []
    for video in desired:
        if remaining[video]:
            additions.append(video)
            remaining[video] -= 1
    return additions, any(present[v] or wanted[v] > 1 for v in additions)


def excess_occurrences(tracks, desired):
    wanted, kept, extras = Counter(desired), Counter(), []
    for track in tracks:
        video = track['videoId']
        if kept[video] < wanted[video]:
            kept[video] += 1
        else:
            if wanted[video] and not track.get('setVideoId'):
                raise ValueError("YouTube has not exposed the repeated item's removal handle")
            extras.append(track)
    return extras


def occurrence_order(tracks, desired):
    if Counter(t['videoId'] for t in tracks) != Counter(desired):
        raise ValueError('YouTube did not restore all playlist occurrences. Retry Undo to continue.')
    occurrences = defaultdict(deque)
    for index, track in enumerate(tracks):
        occurrences[track['videoId']].append(index)
    return [occurrences[video].popleft() for video in desired]


def collection_eligible(*, quarantined, first_seen, recency, now, grace_s, played_fraction=.5):
    created = first_seen or now
    if not quarantined or now - created < grace_s:
        return False
    played = sum(at is not None and at >= created for at in recency)
    return not recency or played / len(recency) < played_fraction


def retry_state(account, remote, now, previous, *, first_delay=3600, max_delay=86400):
    attempt = (previous or {}).get('attempts', 0) + 1
    delay = min(max_delay, first_delay * 2 ** min(attempt - 1, 5))
    return {**(previous or {}), 'identity': account, 'ytm': remote, 'attempts': attempt,
            'last_attempt_at': now, 'next_retry_at': now + delay}
