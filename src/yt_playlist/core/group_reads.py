"""Composition for read-only group export and installed-format inspections."""
from threading import RLock
from types import SimpleNamespace

from yt_playlist.controllers.compact_taste_groups import CompactTasteGroups
from yt_playlist.controllers.group_retention import GroupRetention
from yt_playlist.core import group_documents, policy_documents, profile_files
from yt_playlist.repos.compact_taste_groups import CompactTasteGroupsRepo
from yt_playlist.repos.group_retention_queries import GroupRetentionRepo
from yt_playlist.repos.group_queries import canonical_version


def service(store):
    return CompactTasteGroups(CompactTasteGroupsRepo(store), group_documents.GroupFiles(
        group_documents.directory(store.conn), policy_documents.directory(store.conn)),
        profile_files.ProfileFiles(profile_files.directory(store.conn)),
        GroupRetention(GroupRetentionRepo(store), lambda: store.read_snapshot()))


def _reader(conn):
    return service(SimpleNamespace(conn=conn, _lock=RLock(), _read_only=True))


def head(conn, *, members=False):
    return _reader(conn).head(members=members)


def head_generation(conn):
    return _reader(conn).head_generation()


def head_checkpoint(conn):
    return _reader(conn).head_checkpoint()


def definitions(conn, generation, *, members=False):
    return _reader(conn).definitions(generation, members=members)


def definition(conn, version, *, generation=None):
    return _reader(conn).definition(version, generation=generation)


def correction_definition(conn, version, *, generation=None):
    return _reader(conn).correction_definition(version, generation=generation)


def learning_head(conn):
    return _reader(conn).learning_head()


def leaf_members(conn, generation):
    return _reader(conn).leaf_members(generation)


def lineage(conn):
    return _reader(conn).lineage()


def successors(conn, version, *, limit=512):
    return _reader(conn).successors(version, limit=limit)


def legacy_versions(conn):
    return _reader(conn).legacy_versions()


def retention_plan(conn):
    from yt_playlist.core import group_retention_policy
    from yt_playlist.repos import group_publication
    repository = GroupRetentionRepo(SimpleNamespace(conn=conn, _lock=RLock(), _read_only=True))
    return group_retention_policy.plan(repository, batch=group_publication.BATCH_ROWS)


def retention_closure(conn):
    from yt_playlist.core import group_retention_policy
    repository = GroupRetentionRepo(SimpleNamespace(conn=conn, _lock=RLock(), _read_only=True))
    state = repository.retention_state()
    roots = group_retention_policy.roots(recommendations=state['recommendations'], checkpoint=state['checkpoint'])
    return group_retention_policy.closure(repository, roots)
