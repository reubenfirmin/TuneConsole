"""Story acquisition, private readers and provider access binding."""
from contextlib import contextmanager
from types import SimpleNamespace

from yt_playlist.controllers.story_preparation import StoryPreparation
from yt_playlist.providers import wikipedia
from yt_playlist.providers.base import LookupUnavailable
from yt_playlist.rec import story_rules, transient
from yt_playlist.util import genre_catalog, genre_family_data, net


def capture_taxonomy():
    records, labels = genre_catalog.catalog()
    families, _, _, names = genre_family_data.load()
    return story_rules.StoryTaxonomy(
        canonical={label: records[key]['name'] for label, (key, _, _) in labels.items()},
        families=families,
        family_names=frozenset(names) | {label.lower() for label in genre_catalog.family_labels().values()})


def stories(store, *, taxonomy=None):
    taxonomy = capture_taxonomy() if taxonomy is None else taxonomy
    @contextmanager
    def snapshot():
        with store.read_snapshot() as source:
            yield SimpleNamespace(inputs=stories(source.store, taxonomy=taxonomy),
                                  publish=source.publish, revision=source._changes)

    def fetch(subject, now, fetch_fn):
        with wikipedia.access(store), net.probe_errors() as errors:
            try:
                kwargs = ({'artist_genres': subject['recording_genres']}
                          if fetch_fn is None and 'recording_genres' in subject else {})
                payload = (fetch_fn or wikipedia.fetch_summary)(subject['kind'], subject['display'], **kwargs)
            except LookupUnavailable as exc:
                exc.retry_at = max([getattr(exc, 'retry_at', 0), *(net.retry_after(e, now) for e in errors)])
                raise
        if errors:
            exc = LookupUnavailable('Wikipedia lookup unavailable')
            exc.retry_at = max(net.retry_after(e, now) for e in errors)
            raise exc
        return payload

    return StoryPreparation(charts=store.charts, genres=store.genres, wiki=store.wiki,
        proposals=getattr(store, 'rec_proposals', None), ledger=getattr(store, 'rec_ledger', None),
        leans=lambda now: transient.facet_leans(store, now), snapshot=snapshot, fetch=fetch, taxonomy=taxonomy)
