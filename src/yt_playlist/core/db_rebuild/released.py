"""Explicit v0.1.18 input contract, sharing current domain writers with schema 93.

This is an import into a newly created database. It never runs the historical
DDL/data replacement chain, and never changes the released serving database.
"""
from functools import partial

from . import development, released_catalog, released_groups, released_learning, released_outputs
from .build import Stage


CONVERTED = (development.CONVERTED | released_catalog.CONVERTED | released_groups.CONVERTED |
             released_learning.CONVERTED | released_outputs.CONVERTED)
DERIVED = development.DERIVED


def _discovery(reader, target, row, *, table):
    value = dict(row)
    if table=='discovered_artists':
        from .. import genre_identity
        label = value.pop('genre')
        key,reason = genre_identity.resolve(target,label)
        value.update(genre_key=key,unresolved_genre=None if key is not None else label)
        if label is not None:
            target.execute('INSERT INTO genre_classification_legacy(artist_id,raw_label,genre_key,resolution_reason) '
                           'VALUES (?,?,?,?)',(row['artist_id'],label,key,reason))
    from ..migrations import v101_discovery_subjects as discovery
    discovery.copy_row(target,table,value)


def stages():
    yield Stage('genre_preferences',released_learning.preferences)
    yield from released_catalog.stages()
    yield from released_learning.stages()
    yield from released_outputs.policy_stages()
    yield from released_groups.stages()
    # Reuse the development mappings after their released predecessors exist.
    for stage in development.stages():
        if stage.name=='rec_proposals':
            continue
        if stage.name in ('artist_scans','discovered_artists','discovery_sources','discovery_artist_sources',
                          'discovery_visible_impressions'):
            # Capture the released rows before adapting their genre columns.
            yield development.rows(stage.name,partial(_discovery,table=stage.name),
                capture=partial(development._retain_discovery,table=stage.name))
        else:
            yield stage
    yield from released_outputs.output_stages()


def verify(reader, target):
    released_catalog.verify(reader,target)
