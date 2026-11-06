"""Exercise the paid-request controller against migration-test connections."""
from functools import partial
from threading import RLock
from types import SimpleNamespace

from yt_playlist.controllers.album_descriptions import AlbumDescriptions
from yt_playlist.core import album_description_files, policy_documents
from yt_playlist.repos.album_descriptions import AlbumDescriptionsRepo


def service(conn):
    database = SimpleNamespace(conn=conn, _lock=RLock(), _db_path=None)
    return AlbumDescriptions(AlbumDescriptionsRepo(database), album_description_files.read,
        partial(album_description_files.publish, album_description_files.directory(conn)),
        partial(policy_documents.publish, policy_documents.directory(conn)))


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
