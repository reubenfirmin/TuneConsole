"""Shared-model composition for offline owners and rebuild connections."""
from yt_playlist.controllers.vibe_models import VibeModels, same_receipt as _same_receipt
from . import vibe_model_queries as sql
from . import file_paths, vibe_model_files as files
from .vibe_model_files import HEAD_PREFIX, STATE_PREFIX, REVISION, MAX_BYTES, relative, encode, receipt, measurement_ids, is_receipt


def directory(conn):
    return file_paths.directory(conn, 'vibe-models')


def service(conn, provenance=None):
    return VibeModels(sql.VibeModelQueries(conn), files.VibeModelFiles(directory(conn)), provenance)


def read(conn, generation):
    return service(conn).read(generation)


def stage(conn, value):
    return files.stage(directory(conn), value)


def collect(conn, now, limit=16):
    return service(conn).collect(now, limit)


def references(conn):
    return (directory(conn) / relative(generation) for generation in sql.referenced_generations(conn))


def same_receipt(conn, prediction, value):
    return _same_receipt(sql.receipt_marker(conn, prediction), value)


available, linked, subject = sql.available, sql.linked, sql.subject
