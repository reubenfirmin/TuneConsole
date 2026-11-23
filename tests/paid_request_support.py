"""Exercise the paid-request controller against migration-test connections."""
from threading import RLock
from types import SimpleNamespace

from yt_playlist.controllers.album_descriptions import AlbumDescriptions
from yt_playlist.controllers.policies import Policies
from yt_playlist.core import policy_documents
from yt_playlist.core.policy_files import PolicyFiles
from yt_playlist.repos.album_descriptions import AlbumDescriptionsRepo
from yt_playlist.repos.policies import PolicyRepo


def service(conn):
    database = SimpleNamespace(conn=conn, _lock=RLock(), _db_path=None)
    policy_root = policy_documents.directory(conn)
    return AlbumDescriptions(AlbumDescriptionsRepo(database),
        Policies(PolicyRepo(database), PolicyFiles(policy_root, policy_root / 'history'), lambda: database._lock))


def claim(conn, album_id, version, credential, daily_limit, now, *, configuration=None):
    return service(conn).claim_version(album_id, version, credential, daily_limit, now, configuration=configuration)


def authorize(conn, request_id, token, credential, daily_limit, now):
    return service(conn).authorize(request_id, token, credential, daily_limit, now)


def finish(conn, request_id, token, record, now):
    return service(conn).finish(request_id, token, record, now)


def usage(conn, now):
    return service(conn).requests_today(now)


def result(conn, request_id):
    return service(conn).result(request_id)
