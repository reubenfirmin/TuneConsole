"""Compatibility entry points for prepared stories and detached editorial rules."""
from yt_playlist.library.story_composition import capture_taxonomy, stories
from yt_playlist.rec import story_rules, transient
from yt_playlist.rec.story_rules import (
    FAVORITE_TOP_N, GENRE_OBVIOUS_SHARE, PREWARM_POOL, MAX_STORIES, MAX_SUMMARY_FETCHES,
    MAX_CANDIDATES, SURFACE, SCHEMA, MAX_AGE_S, RECENT_WINDOW_S, STORY_COOLDOWN_S,
    EDITION_HOLD_S, SONG_WINDOW_S, FAMILIAR_GENRE_AGE_S, FAMILIAR_GENRE_PLAYS,
    SUBGENRE_TIEBREAK, CLUSTER_DEPTH, _COMMON_SCENES, _UMBRELLA_GENRES,
    _INTRODUCTORY_GENRES, _norm, _variety_order, _mentions_genre,
    _article_weight, _editorial_order, editorial_spreads,
)


def _specific_genre(name):
    return story_rules._specific_genre(name, taxonomy=capture_taxonomy())


def _specific_genre_card(card):
    return story_rules._specific_genre_card(card, taxonomy=capture_taxonomy())


def _usable_article(card):
    return story_rules._usable_article(card, taxonomy=capture_taxonomy())


def _genre_key(name):
    return story_rules._genre_key(name, taxonomy=capture_taxonomy())


def _genre_can_lead(subject, row, familiar):
    return story_rules._genre_can_lead(subject, row, familiar, taxonomy=capture_taxonomy())


def _matching_summary(subject, row):
    return story_rules._matching_summary(subject, row, taxonomy=capture_taxonomy())


def story_groups(cards):
    return story_rules.story_groups(cards, taxonomy=capture_taxonomy())


def subject_color(subject):
    return story_rules.subject_color(subject, taxonomy=capture_taxonomy())


def ranked_subjects(store, now):
    return stories(store).ranked(now)


def _baseline_genre_shares(store):
    return stories(store).baseline()


def _familiar_genres(store, now):
    return stories(store).familiar(now)


def _connected_subjects(store, subjects, familiar=frozenset(), now=None):
    return stories(store).connected(subjects, familiar, now)


def _fetch_plan(store, groups):
    return stories(store).fetch_plan(groups)


def _publish_pool(store, now):
    return stories(store).publish_pool(now)


def prewarm_pool(store, now, fetch_fn=None, limit=PREWARM_POOL, *, subjects=None, fetch_limit=None):
    return stories(store).prewarm(now, fetch_fn, limit, subjects=subjects, fetch_limit=fetch_limit)


def prepare(store, now, fetch_fn=None):
    return stories(store).prepare(now, fetch_fn)


def refresh_due(store, now):
    return stories(store).refresh_due(now)


def load(store, now, epoch=0, schedule=lambda: None):
    return stories(store).load(now, epoch, schedule)


def refresh_summary(store, subj, now, fetch_fn):
    return stories(store).refresh_summary(subj, now, fetch_fn)


def _genres_in_token(store, token):
    return stories(store).genres_in_token(token)


def _story_songs(store, card, group, now):
    return stories(store).songs(card, group, now)


def decorate(store, subj):
    return stories(store).decorate(subj)
