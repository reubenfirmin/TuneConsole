"""Radio ranking on captured similarities, taxonomy decisions and musical facts."""
from yt_playlist.rec import musical_character, radio_intensity, radio_tuning


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
                    now_share=0, now_gain=0):
    score, continuity = .2 * prior, 1 - breadth
    if recent and continuity:
        weights = [.8 ** age for age in range(len(recent))]
        score += continuity * .3 * sum(w * value for w, value in zip(weights, recent)) / sum(weights)
        score += continuity * .25 * recent[0]
    if target is not None and continuity:
        score += continuity * (.2 + .9 * tuning['theme']) * target
    familiarity = min(1., (track.get('plays') or 0) / 10 + .5 * bool(track.get('liked'))
                      + .3 * bool(track.get('_radio_comfort')))
    score += .7 * tuning['comfort'] * familiarity
    score -= 1.4 * abs(tuning['energy'] - .5) * abs(musical_character.intensity(track) - tuning['energy'])
    score += genre_bonus + sum(strength * max(0, similarity - .45) for strength, similarity in feedback)
    return radio_tuning.now_bonus(score, now_share, now_gain)


def choice_tier(tracks, decisions, *, energy, breadth):
    tracks = [t for t in tracks if decisions[t['key']]['allowed']]
    tracks = radio_intensity.narrow(tracks, energy)
    if not tracks:
        return []
    if breadth == 1:
        contrast = max(decisions[t['key']]['contrast'] for t in tracks)
        tracks = [t for t in tracks if decisions[t['key']]['contrast'] == contrast]
    fit = [t for t in tracks if decisions[t['key']]['fits']]
    tracks = fit or tracks
    gap = max(decisions[t['key']]['artist_gap'] for t in tracks)
    return [t for t in tracks if decisions[t['key']]['artist_gap'] == gap]
