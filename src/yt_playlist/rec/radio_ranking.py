"""Radio ranking on captured similarities, taxonomy decisions and musical facts."""
from heapq import nlargest

from yt_playlist.rec import journeys, musical_character, radio_rotation, radio_tuning
from yt_playlist.util import genre_map

SEQUENCING_LOOKAHEAD = 8
SEQUENCING_SCORE_RANGE = .18
ENERGY_ARC_SPAN = 6


def eligible_page(rows, seen, excluded, allowed, limit):
    """Select a page from supplied eligibility and exact recording/upload facts."""
    if limit <= 0:
        return []
    selected, used = [], set(seen)
    for row in rows:
        track = dict(row)
        if track['key'] in used or track['video_id'] in excluded or not allowed(track):
            continue
        used.add(track['key'])
        selected.append(track)
        if len(selected) == limit:
            break
    return selected


def candidate_score(track, *, prior, recent, target, feedback, tuning, breadth, genre_bonus,
                    now_share=0, now_gain=0, group_share=0):
    score, continuity = .2 * prior + .15 * group_share, 1 - breadth
    if recent and continuity:
        weights = [.8 ** age for age in range(len(recent))]
        score += continuity * .3 * sum(w * value for w, value in zip(weights, recent)) / sum(weights)
        score += continuity * .25 * recent[0]
    if target is not None and continuity:
        score += continuity * (.2 + .9 * tuning['theme']) * target
    familiarity = min(1., (track.get('plays') or 0) / 10 + .5 * bool(track.get('liked'))
                      + .3 * bool(track.get('_radio_comfort')))
    score += .7 * tuning['comfort'] * familiarity
    score += genre_bonus + sum(strength * max(0, similarity - .45) for strength, similarity in feedback)
    return radio_tuning.now_bonus(score, now_share, now_gain)


def _sequence_features(track, axes):
    genre = track.get('genre') or ''
    year = str(track.get('year') or '')
    intensity = (track['_radio_intensity'] if '_radio_intensity' in track
                 else musical_character.intensity_evidence(track)[0])
    return {'artist': track.get('artist'), 'artist_ids': track.get('artist_ids'),
            'genre': genre, 'family': axes.get(genre, (None, None))[0] or genre_map.family(genre),
            'energy': intensity,
            'decade': int(year) // 10 * 10 if year.isascii() and year.isdecimal() and len(year) == 4 else None,
            'plays': track.get('plays') or 0, 'recency': track.get('_radio_last_started') or 0}


def select_next(eligible, history, *, comfort, now, score, similarity, other_anchors, rng,
                journey=None, axes=None):
    """Select within the eligible listening rotation, with bounded tie-breaking."""
    source = radio_rotation.source(eligible, history, comfort)
    def rank(track):
        return (score(track) - radio_rotation.repetition(track, now)
                - .8 * max((similarity(track, anchor) for anchor in other_anchors), default=0)
                + rng.random() * .02)

    if journey:
        ranked = nlargest(SEQUENCING_LOOKAHEAD, ((rank(track), track) for track in source),
                          key=lambda row: row[0])
        floor = ranked[0][0] - SEQUENCING_SCORE_RANGE
        shortlist = [track for value, track in ranked if value >= floor]
        feat = lambda track: _sequence_features(track, axes or {})
        chosen = journeys.continuation_choice(shortlist, journey, rng.randrange(2**32), feat,
            previous=feat(history[-1]) if history else None, position=max(0, len(history) - 1),
            span=ENERGY_ARC_SPAN)
    else:
        chosen = max(source, key=rank)
    return {**chosen, '_radio_rotation_slot': chosen['_radio_rotation']}


def choice_tier(tracks, decisions, *, breadth):
    tracks = [t for t in tracks if decisions[t['key']]['allowed']]
    if not tracks:
        return []
    if breadth == 1:
        contrast = max(decisions[t['key']]['contrast'] for t in tracks)
        tracks = [t for t in tracks if decisions[t['key']]['contrast'] == contrast]
    fit = [t for t in tracks if decisions[t['key']]['fits']]
    tracks = fit or tracks
    gap = max(decisions[t['key']]['artist_gap'] for t in tracks)
    return [t for t in tracks if decisions[t['key']]['artist_gap'] == gap]
