"""Composition for read-only group export and installed-format inspections."""
from threading import RLock
from types import SimpleNamespace

from yt_playlist.controllers.compact_taste_groups import CompactTasteGroups
from yt_playlist.core import group_documents, policy_documents, profile_files
from yt_playlist.repos.compact_taste_groups import CompactTasteGroupsRepo
from yt_playlist.repos.group_retention import GroupRetentionRepo
from yt_playlist.repos.group_queries import canonical_version
from yt_playlist.controllers.policies import Policies
from yt_playlist.core.policy_files import PolicyFiles
from yt_playlist.core import file_paths
from yt_playlist.repos.policies import PolicyRepo


def service(store):
    return CompactTasteGroups(CompactTasteGroupsRepo(store), group_documents.GroupFiles(
        group_documents.directory(store.conn), policy_documents.directory(store.conn)),
        profile_files.ProfileFiles(profile_files.directory(store.conn)),
        GroupRetentionRepo(store),
        Policies(PolicyRepo(store), PolicyFiles(policy_documents.directory(store.conn),
            file_paths.directory(store.conn, 'policy-history')), lambda: store._lock))


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


def legacy_versions(conn):
    return _reader(conn).legacy_versions()
