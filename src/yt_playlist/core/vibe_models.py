"""Shared-model composition for offline owners and rebuild connections."""
from yt_playlist.controllers.vibe_models import VibeModels, same_receipt as _same_receipt
from . import vibe_model_queries as sql
from . import file_paths, vibe_model_files as files
from .schema_contract import tokens
from .schema_inspection import definitions
from .vibe_model_files import HEAD_PREFIX, STATE_PREFIX, REVISION, MAX_BYTES, relative, encode, receipt, measurement_ids, is_receipt


def directory(conn):
    return file_paths.directory(conn, 'vibe-models')


class OfflineModelQueries(sql.VibeModelQueries):
    def __init__(self, conn):
        super().__init__(conn)
        schema = definitions(conn, ('vibe_model_file_writes', 'ix_vibe_model_file_identity', 'vibe_definitions'))
        self.prior_file_ids = 'vibe_model_file_writes' in schema
        self.has_file_ids = 'ix_vibe_model_file_identity' in schema
        self.has_inline_heads = 'joint_head' in tokens(schema.get('vibe_definitions'))

    def file_reference(self, generation):
        if self.has_file_ids:
            return super().file_reference(generation)
        if self.prior_file_ids:
            row = self.conn.execute("SELECT id FROM vibe_model_file_writes WHERE generation=? AND file_status='ready' "
                                    'ORDER BY id DESC LIMIT 1', (bytes.fromhex(generation),)).fetchone()
            return row[0] if row else None
        return None

    def file_digest(self, identity):
        if self.has_file_ids:
            return super().file_digest(identity)
        if self.prior_file_ids:
            row = self.conn.execute('SELECT generation FROM vibe_model_file_writes WHERE id=?', (identity,)).fetchone()
            return row[0].hex() if row else None
        return None

    def referenced(self, generation):
        return super().referenced(generation, inline_heads=self.has_inline_heads)


def service(conn, provenance=None):
    return VibeModels(OfflineModelQueries(conn), files.VibeModelFiles(directory(conn)), provenance)


def read(conn, generation):
    return service(conn).read(generation)


def stage(conn, value):
    return files.stage(directory(conn), value)


def collect(conn, now, limit=16):
    return service(conn).collect(now, limit)


def available(conn):
    return bool(definitions(conn, ('prediction_model_generations', 'predictions_provenance_immutable')))


def _referenced_generations(conn, *, inline_heads):
    seen = set()
    # Expired history no longer requires its model. Its SQL identity lasts
    # until bounded prediction-history retirement removes that calculation.
    if definitions(conn, ('prediction_model_generations',)):
        sql = ("SELECT DISTINCT g.generation FROM prediction_model_generations g "
               "JOIN prediction_provenance p ON p.prediction_id=g.prediction_id WHERE p.state='ready'")
    else:
        sql = ("SELECT DISTINCT model_generation FROM predictions "
               "WHERE model_generation IS NOT NULL AND provenance_state='ready'")
    for generation, in conn.execute(sql):
        seen.add(generation.hex())
        yield generation.hex()
    heads = (conn.execute("SELECT joint_head FROM vibe_definitions WHERE joint_head IS NOT NULL")
             if inline_heads else conn.execute("SELECT value FROM settings WHERE key GLOB ?", (HEAD_PREFIX + '*',)))
    for generation, in heads:
        if generation and generation not in seen:
            seen.add(generation)
            yield generation


def references(conn):
    queries = OfflineModelQueries(conn)
    root = directory(conn)
    for generation in _referenced_generations(conn, inline_heads=queries.has_inline_heads):
        yield root / relative(queries.file_reference(generation) or generation)


def same_receipt(conn, prediction, value):
    return _same_receipt(sql.receipt_marker(conn, prediction), value)


def linked(conn, prediction):
    return sql.linked(conn, prediction)


subject = sql.subject
