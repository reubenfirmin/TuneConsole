"""SQL manifests for account workflow settings."""
from yt_playlist.core import workflow_settings as workflows
from yt_playlist.repos import collection_file_writes


def documents(conn, kind, account):
    return workflows.documents(conn, kind, account)


def keys(conn, prefix):
    if not any(name.startswith(prefix) or prefix.startswith(name) for name in (workflows.PLAYLIST, workflows.ALBUM)):
        return []
    result = [(workflows.PLAYLIST if kind == 'playlist' else workflows.ALBUM)+('' if account is None else ':'+str(account))
              for kind, account in workflows.owners(conn)]
    return [key for key in result if key.startswith(prefix)]


def register(conn, plans):
    revision = None
    for kind, prepared, reference in plans:
        if kind == 'receipt':
            revision = workflows.register_receipt(conn, prepared, reference)
        else:
            source, account = prepared
            workflows.delete(conn, source, account)
        if reference is not None:
            collection_file_writes.complete(conn, prepared['file_id'], 'spotify-transfer', prepared['identity'], reference)
    return revision
