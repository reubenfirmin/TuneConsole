"""Prepare connected reading from recent listening, with room for unfamiliar subjects.

Recent plays establish eligibility; taste leans rank the eligible subjects.
Visible story appearances give other subjects a turn without exhausting a thin
pool. Common genres provide context inside artist-led spreads.
"""
from dataclasses import dataclass, field
import re
from types import MappingProxyType
import unicodedata

from yt_playlist.util.genre_theme import genre_tint
from yt_playlist.util.matching import is_remix
from yt_playlist.util.wikipedia_subject import matches_subject

FAVORITE_TOP_N = 15        # all-time top artists treated as settled favorites (excluded)
GENRE_OBVIOUS_SHARE = 0.20  # a genre at or above this share of the library is fully "obvious"
PREWARM_POOL = 12          # bound cached candidates for exposure-aware, held story editions
MAX_STORIES = 9
MAX_SUMMARY_FETCHES = 3    # network lookups per worker pass; yield between small batches
MAX_CANDIDATES = 48
SURFACE = "into_recently"
SCHEMA = 10
MAX_AGE_S = 1800
RECENT_WINDOW_S = 14 * 86400
STORY_COOLDOWN_S = 7 * 86400
EDITION_HOLD_S = 6 * 3600
SONG_WINDOW_S = 30 * 86400
FAMILIAR_GENRE_AGE_S = 90 * 86400
FAMILIAR_GENRE_PLAYS = 12
_COMMON_SCENES = frozenset(('goa trance', 'psytrance', 'psychedelic trance', 'minimal',
                            'minimal techno', 'deep house', 'progressive house', 'acid', 'acid house'))
_UMBRELLA_GENRES = frozenset(('rock', 'pop', 'electronic', 'jazz', 'folk', 'country',
                             'soul', 'funk', 'world', 'latin', 'hip hop', 'metal', 'classical'))
_INTRODUCTORY_GENRES = frozenset((
    'electronic', 'electronica', 'electronic dance', 'music', 'other', 'unknown',
    'rock', 'hip hop', 'hip-hop', 'soul', 'funk', 'r&b', 'rhythm and blues',
    'folk', 'country', 'world', 'latin', 'dance', 'drum and bass', 'drum & bass',
    'indie', 'indie rock', 'indie pop', 'alternative rock', 'classic rock',
    'hard rock', 'soft rock', 'progressive rock', 'psychedelic rock',
    'rock and roll', 'heavy metal', 'punk rock', 'electronic dance music', 'edm', 'reggae',
))
SUBGENRE_TIEBREAK = 1.05    # nudge specific subgenres above their broad family
CLUSTER_DEPTH = 2          # how many rings the "explore in your catalog" CTA grows (kept tight)


@dataclass(frozen=True)
class StoryTaxonomy:
    canonical: dict = field(default_factory=dict)
    families: dict = field(default_factory=dict)
    family_names: frozenset = frozenset()

    def __post_init__(self):
        object.__setattr__(self, 'canonical', MappingProxyType(dict(self.canonical)))
        object.__setattr__(self, 'families', MappingProxyType(dict(self.families)))
        object.__setattr__(self, 'family_names', frozenset(self.family_names))

    @staticmethod
    def identity(name):
        return ' '.join(''.join(ch if ch.isalnum() else ' ' for ch in
            unicodedata.normalize('NFKC', name or '').casefold()).split())

    def canonical_name(self, name):
        return self.canonical.get(self.identity(name), (name or '').strip())

    def family(self, name):
        return self.families.get(self.identity(name), f'other:{_norm(name)}') if name else ''


EMPTY_TAXONOMY = StoryTaxonomy()



def _norm(s):
    return (s or "").strip().lower()


def _specific_genre(name, *, taxonomy=EMPTY_TAXONOMY):
    """Stories introduce particular scenes and subgenres, rather than genre primers."""
    name = (_norm(name).removeprefix('other:').removesuffix(' (music)')
            .removesuffix(' (genre)').removesuffix(' music'))
    canonical = _norm(taxonomy.canonical_name(name) or name)
    families = taxonomy.family_names
    return bool(name) and canonical not in _INTRODUCTORY_GENRES | families and name not in families


def _specific_genre_card(card, *, taxonomy=EMPTY_TAXONOMY):
    if card.get('kind') != 'genre':
        return True
    title = card.get('title') or card['display']
    return (all(_specific_genre(name, taxonomy=taxonomy) for name in
                (card['display'], card['subject'].removeprefix('genre:'), title))
            and not _norm(title).startswith(('list of ', 'index of ')))


def _usable_article(card, *, taxonomy=EMPTY_TAXONOMY):
    """Common genres are useful companions; wrong subjects and lists are not."""
    title = card.get('title')
    return (not _norm(title).startswith(('list of ', 'index of '))
            and (not title or matches_subject(card['kind'], card['display'], title,
                                             canonical_name=taxonomy.canonical_name)))


def _baseline_genre_shares(corpus, axes) -> dict:
    """Library share per facet token (family and subgenre), from the owned-track genre distribution.
    corpus_distribution is keyed by raw genre; fold it into the same family/subgenre tokens the
    transient facets use so shares are directly comparable."""
    total = sum(corpus.values()) or 1
    counts: dict = {}
    for g, c in corpus.items():
        fam, sub = axes.get(g, ('', None))
        if fam:
            counts[fam] = counts.get(fam, 0) + c
        if sub and sub != fam:
            counts[sub] = counts.get(sub, 0) + c
    return {tok: c / total for tok, c in counts.items()}


def ranked_subjects(*, leans, activity, favorites, genre_base, axes, taxonomy=EMPTY_TAXONOMY) -> list:
    """Rank recently heard subjects by taste, retaining common genre context."""
    recent_artists = {_norm(name) for name in activity['artist']}
    favorites = {_norm(a['artist']) for a in favorites}
    scored = []
    for facet, strength in leans.items():
        if strength <= 0:
            continue
        kind, _, name = facet.partition(":")
        if kind == "artist":
            if _norm(name) in favorites or _norm(name) not in recent_artists:
                continue
            score, display = strength, name
        else:
            continue
        scored.append((score, kind, facet, display))

    # Actual tags supply context even when the model only has a broad family
    # lean. Do not turn aggregate tokens such as rock-indie into fake articles.
    genres = {_genre_key(name, taxonomy=taxonomy): name for name in activity['genre']}
    for key, raw in genres.items():
        if key in ('electronic', 'electronica', 'electronic music', 'electronic dance music',
                   'music', 'other', 'unknown', 'dance'):
            continue
        family, sub = axes.get(raw, ('', None))
        strength = max(leans.get(f'genre:{key}', 0), leans.get(f'genre:{sub}', 0),
                       leans.get(f'genre:{family}', 0) * .5)
        if strength <= 0:
            if _specific_genre(key, taxonomy=taxonomy):
                continue
            strength = .01  # descriptive context for a recent artist, never a genre lead
        share = genre_base.get(sub or family, 0)
        novelty = max(.15, 1.0 - min(1.0, share / GENRE_OBVIOUS_SHARE))
        score = strength * novelty * (SUBGENRE_TIEBREAK if _specific_genre(key, taxonomy=taxonomy) else .5)
        scored.append((score, 'genre', f'genre:{key}', key))

    scored.sort(key=lambda item: (-item[0], item[2]))
    return [{"kind": k, "subject": f, "display": d} for (_s, k, f, d) in scored]


def _variety_order(subjects, appearances, now):
    def priority(item):
        rank, subject = item
        subject = subject['subject']
        seen = appearances.get(subject, {})
        last = seen.get('last_shown')
        cooling = last is not None and now - last < STORY_COOLDOWN_S
        # Within the cooldown, prefer the least recently seen subjects. A
        # stronger taste score cannot keep the same five artists at the front.
        return (cooling, last if cooling else 0, rank + min(5, seen.get('views', 0)), subject)

    return [subject for _, subject in sorted(enumerate(subjects), key=priority)]


def _genre_key(name, *, taxonomy=EMPTY_TAXONOMY):
    name = _norm(name).removeprefix('other:')
    return _norm(taxonomy.canonical_name(name) or name)


def _familiar_genres(distribution, *, taxonomy=EMPTY_TAXONOMY):
    """Years of listening, including imports, establish familiarity with a genre."""
    counts = {}
    for genre, plays in distribution.items():
        key = _genre_key(genre, taxonomy=taxonomy)
        counts[key] = counts.get(key, 0) + plays
    return {key for key, plays in counts.items() if plays >= FAMILIAR_GENRE_PLAYS}


def _genre_can_lead(subject, row, familiar, *, taxonomy=EMPTY_TAXONOMY):
    keys = {_genre_key(subject['subject'].removeprefix('genre:'), taxonomy=taxonomy),
            _genre_key(row.get('title') or subject['display'], taxonomy=taxonomy)}
    return bool(row.get('thumbnail') and _specific_genre_card({**row, **subject}, taxonomy=taxonomy)
                and not keys.intersection(_COMMON_SCENES | familiar))


def _matching_summary(subject, row, *, taxonomy=EMPTY_TAXONOMY):
    if row and row.get('title') and not _usable_article({**row, **subject}, taxonomy=taxonomy):
        return None
    return row


def _mentions_genre(extract, genre):
    # Phrase boundaries keep e.g. "dub" out of "Dublin". The resolved article
    # title disambiguates short labels such as minimal (minimal techno).
    words = re.findall(r'[^\W_]+', genre.casefold())
    text = ' '.join(re.findall(r'[^\W_]+', extract.casefold()))
    return bool(words) and (' ' + ' '.join(words) + ' ') in (' ' + text + ' ')


def _connected_subjects(subjects, *, rows, activity, familiar=frozenset(), fresh_misses=frozenset(),
                        taxonomy=EMPTY_TAXONOMY):
    """Plan small reading groups using the recordings actually heard recently.

    A shared recommendation family or an artist's career genre is too loose a
    connection. Umbrella tags also need article corroboration and an unremixed
    example. A remix alone cannot establish the original artist's scene.
    """
    # Inspect a wider ranked set before budgeting article work. Otherwise a
    # run of standalone genres can hide an artist and their exact genre below
    # the cutoff, even when both articles are already cached.
    subjects = subjects[:MAX_CANDIDATES * 3]
    subjects = [s for s in subjects if s['subject'] not in fresh_misses]
    recordings = {_norm(name): songs for name, songs in activity['recordings'].items()}
    genres = [s for s in subjects if s['kind'] == 'genre']
    related = {}
    assigned = set()

    def readability(subject):
        row = rows.get(subject['subject'])
        usable = bool(row and row.get('found') and row.get('extract'))
        missed = bool(row and not row.get('found'))
        return (not usable, missed)

    # An earlier negative-cache miss must not occupy one of the two companion
    # slots ahead of a readable artist further down the recent candidate list.
    for subject in sorted((s for s in subjects if s['kind'] == 'artist'), key=readability):
        songs = recordings.get(_norm(subject['display']), [])
        keys = {_genre_key(song['genre'], taxonomy=taxonomy) for song in songs if song['genre']}
        unremixed = [song for song in songs if not is_remix(song['title'])]
        artist = rows.get(subject['subject']) or {}
        matches = []
        for rank, genre in enumerate(genres):
            row = rows.get(genre['subject']) or {}
            token = _genre_key(genre['subject'].removeprefix('genre:'), taxonomy=taxonomy)
            title = row.get('title') or genre['display']
            exact = bool(keys.intersection((token, _genre_key(title, taxonomy=taxonomy))))
            if token in _UMBRELLA_GENRES:
                exact = (exact and _mentions_genre(artist.get('extract') or '', token)
                         and any(_genre_key(song['genre'], taxonomy=taxonomy) == token for song in unremixed))
            mentioned = (bool(unremixed) and _specific_genre(title, taxonomy=taxonomy)
                         and _mentions_genre(artist.get('extract') or '', title))
            if exact or mentioned:
                matches.append((not exact, rank, genre))
        for _, _, genre in sorted(matches):
            companions = related.setdefault(genre['subject'], [])
            if len(companions) < 2:
                companions.append(subject)
                assigned.add(subject['subject'])
                break
    groups = [[s, *related.get(s['subject'], [])] for s in subjects if s['subject'] not in assigned]
    ranks = {s['subject']: i for i, s in enumerate(subjects)}

    def rank(group):
        leads = [s for s in group if s['kind'] == 'artist'
                 or (_specific_genre(s['display'], taxonomy=taxonomy)
                     and _genre_key(s['display'], taxonomy=taxonomy) not in _COMMON_SCENES | familiar)]
        return min(ranks[s['subject']] for s in leads or group)

    def can_prepare(group):
        if len(group) > 1 or group[0]['kind'] == 'artist':
            return True
        genre = group[0]
        row = rows[genre['subject']]
        if (not _specific_genre(genre['display'], taxonomy=taxonomy)
                or _genre_key(genre['display'], taxonomy=taxonomy) in _COMMON_SCENES | familiar):
            return False
        return not (row and row.get('found')) or _genre_can_lead(genre, row, familiar, taxonomy=taxonomy)

    def ready(group):
        return any(rows[s['subject']] and rows[s['subject']].get('found')
                   and rows[s['subject']].get('extract')
                   and (s['kind'] == 'artist' or _genre_can_lead(s, rows[s['subject']], familiar, taxonomy=taxonomy))
                   for s in group)

    planned, count = [], 0
    for group in sorted(filter(can_prepare, groups), key=lambda group: (not ready(group), rank(group))):
        if count + len(group) <= MAX_CANDIDATES:
            planned.append(group)
            count += len(group)
    return sorted(planned, key=rank)


def story_groups(cards, *, taxonomy=EMPTY_TAXONOMY):
    """Read the worker's connections without querying or inferring anything."""
    by_subject = {card['subject']: card for card in cards}
    groups = {}
    for card in cards:
        anchor = card.get('group', card['subject'])
        # A missing/filtered genre article must not leave artists attached to
        # an invisible heading or imply a connection to a different story.
        if anchor not in by_subject:
            anchor = card['subject']
        groups.setdefault(anchor, []).append(card)
    ordered = []
    for group in groups.values():
        leads = [card for card in group if card.get('lead_allowed', card['kind'] == 'artist')
                 and card.get('thumbnail') and _specific_genre_card(card, taxonomy=taxonomy)]
        lead = max(leads, key=lambda card: (not card.get('recently_shown', False), _article_weight(card)),
                   default=None)
        if lead is not None:
            ordered.append([lead, *[card for card in group if card is not lead]])
    return ordered


def _article_weight(card):
    return min(200, len(card.get('extract', '').split()))


def _editorial_order(groups):
    # Selection handles variety first. Within that edition, put the most
    # developed reading near the top, keeping its connected stories beside it.
    return sorted(groups, key=lambda group: (group[0].get('recently_shown', False),
                                            -(_article_weight(group[0]) + 12 * (len(group) - 1))))


def _fetch_plan(groups, rows):
    """Complete readable spreads before fetching unrelated cold stories."""
    def priority(group):
        warm = []
        for subject in group:
            row = rows.get(subject['subject'])
            if row and row['found'] and row['extract']:
                warm.append(subject)
        if len(group) > 1 and any(s['kind'] == 'artist' for s in warm):
            return 0
        return 1 if warm else 2

    return [subject for group in sorted(groups, key=priority) for subject in group]


def editorial_spreads(groups):
    """Keep related stories together and give independent pieces a magazine rhythm."""
    spreads, singles = [], []

    def flush():
        if not spreads and len(singles) >= 3:
            spreads.append({'layout': 'feature', 'groups': [singles.pop(0)]})
        while singles:
            size = min(3, len(singles))
            spreads.append({'layout': {1: 'feature', 2: 'pair', 3: 'columns'}[size],
                            'groups': singles[:size]})
            del singles[:size]

    for group in groups:
        if len(group) == 1:
            singles.append(group)
        else:
            flush()
            spreads.append({'layout': 'cluster', 'groups': [group]})
    flush()
    return spreads


def load(payload, now, epoch=0, *, taxonomy=EMPTY_TAXONOMY):
    """Serve complete prepared stories; opening a tab never ranks or contacts Wikipedia."""
    try:
        valid = (isinstance(payload, dict) and payload.get("schema") == SCHEMA
                 and isinstance(payload.get("cards"), list)
                 and len(payload["cards"]) <= PREWARM_POOL
                 and isinstance(payload.get("built_at"), (int, float))
                 and all(isinstance(card, dict) and card.get("extract")
                         and (card.get('thumbnail') or card.get('kind') == 'genre')
                         and all(key in card for key in ("display", "subject", "kind", "color", "seed", "genre", "depth"))
                         for card in payload["cards"]))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        return [], True, True
    cards = [card for card in payload['cards'] if _usable_article(card, taxonomy=taxonomy)]
    pending = bool(payload.get("pending")) or len(cards) != len(payload['cards'])
    refresh = pending or now - payload['built_at'] >= MAX_AGE_S
    groups = story_groups(cards, taxonomy=taxonomy)
    if groups:
        offset = epoch % len(groups)
        groups = groups[offset:] + groups[:offset]
    selected, count = [], 0
    for group in groups:
        if count + len(group) <= MAX_STORIES:
            selected.append(group)
            count += len(group)
    return [card for group in _editorial_order(selected) for card in group], pending, refresh


def subject_color(subj, *, taxonomy=EMPTY_TAXONOMY) -> str:
    """The heading tint/glow colour for a picked subject. Genres map by family; artists use accent."""
    if subj["kind"] != "genre":
        return genre_tint(None)
    token = subj["subject"].split(":", 1)[1]
    tint = genre_tint(token)
    return tint if tint != "var(--genre-default)" else genre_tint(taxonomy.family(token))


def _genres_in_token(corpus, token, *, taxonomy=EMPTY_TAXONOMY) -> list:
    """Equivalent stored labels; a shared recommendation family is not the same genre."""
    return [g for g in corpus
            if _genre_key(g, taxonomy=taxonomy) == _genre_key(token, taxonomy=taxonomy)]


def decorate(subj, *, thumbnail=None, representative=None, taxonomy=EMPTY_TAXONOMY) -> dict:
    """Heading colour and catalog seed, with a local image fallback for artist stories.

    A representative artist may seed genre exploration, but their album artwork
    does not illustrate the genre article.
    """
    color = subject_color(subj, taxonomy=taxonomy)
    if subj["kind"] == "artist":
        return {"color": color, "thumbnail": thumbnail,
                "seed": subj["display"], "genre": None}
    token = subj["subject"].split(":", 1)[1]
    rep = representative
    return {"color": color, "thumbnail": None,
            "seed": rep["artist"] if rep else None, "genre": token}
