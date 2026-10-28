"""Stage disposable recommendation bytes without rewriting the last-good row."""

CHUNK_BYTES = 64 * 1024


def statements():
    yield ("CREATE TABLE rec_proposal_cache_builds (cache_id TEXT PRIMARY KEY NOT NULL,"
           "surface TEXT NOT NULL CHECK(surface IN ('mode_bundles','fresh_pool')),"
           "byte_size INTEGER NOT NULL CHECK(byte_size>0),"
           f"chunk_count INTEGER NOT NULL CHECK(chunk_count=(byte_size+{CHUNK_BYTES - 1})/{CHUNK_BYTES}),"
           "written_chunks INTEGER NOT NULL DEFAULT 0 CHECK(written_chunks BETWEEN 0 AND chunk_count),"
           "state INTEGER NOT NULL DEFAULT 0 CHECK(state IN (0,1,2)),"
           "nonempty INTEGER NOT NULL CHECK(nonempty IN (0,1)),"
           "created_at REAL NOT NULL,expires_at REAL NOT NULL,UNIQUE(cache_id,surface))")
    yield 'CREATE INDEX ix_proposal_cache_expiry ON rec_proposal_cache_builds(expires_at)'
    yield ("CREATE TABLE rec_proposal_cache_chunks (cache_id TEXT NOT NULL REFERENCES rec_proposal_cache_builds(cache_id),"
           "chunk_index INTEGER NOT NULL CHECK(chunk_index>=0),payload BLOB NOT NULL "
           f"CHECK(typeof(payload)='blob' AND length(payload) BETWEEN 1 AND {CHUNK_BYTES}),"
           "PRIMARY KEY(cache_id,chunk_index)) WITHOUT ROWID")
    yield ("CREATE TABLE rec_proposal_cache_heads (surface TEXT PRIMARY KEY NOT NULL "
           "REFERENCES rec_proposals(surface) ON DELETE CASCADE,cache_id TEXT NOT NULL,built_at REAL,"
           "FOREIGN KEY(cache_id,surface) REFERENCES rec_proposal_cache_builds(cache_id,surface))")
    yield 'CREATE INDEX ix_proposal_cache_head_build ON rec_proposal_cache_heads(cache_id)'
    yield ("CREATE TRIGGER proposal_cache_build_insert BEFORE INSERT ON rec_proposal_cache_builds "
           "WHEN NEW.state<>0 OR NEW.written_chunks<>0 BEGIN "
           "SELECT RAISE(ABORT,'Recommendation cache must start unpublished'); END")
    yield ("CREATE TRIGGER proposal_cache_build_update BEFORE UPDATE ON rec_proposal_cache_builds WHEN "
           "NEW.cache_id IS NOT OLD.cache_id OR NEW.surface IS NOT OLD.surface OR "
           "NEW.byte_size IS NOT OLD.byte_size OR NEW.chunk_count IS NOT OLD.chunk_count OR "
           "NEW.nonempty IS NOT OLD.nonempty OR NEW.created_at IS NOT OLD.created_at OR "
           "(NEW.written_chunks<>OLD.written_chunks AND NOT (OLD.state=0 AND NEW.state=0 AND "
           "NEW.written_chunks=OLD.written_chunks+1 AND EXISTS(SELECT 1 FROM rec_proposal_cache_chunks "
           "WHERE cache_id=OLD.cache_id AND chunk_index=OLD.written_chunks))) OR "
           "(NEW.state<>OLD.state AND NOT ((OLD.state=0 AND NEW.state=1 AND "
           "NEW.written_chunks=NEW.chunk_count AND (SELECT count(*) FROM rec_proposal_cache_chunks "
           "WHERE cache_id=OLD.cache_id)=NEW.chunk_count) OR (NEW.state=2 AND NOT EXISTS "
           "(SELECT 1 FROM rec_proposal_cache_heads WHERE cache_id=OLD.cache_id)))) "
           "BEGIN SELECT RAISE(ABORT,'Invalid recommendation cache transition'); END")
    yield ("CREATE TRIGGER proposal_cache_chunk_insert BEFORE INSERT ON rec_proposal_cache_chunks "
           "WHEN NOT EXISTS(SELECT 1 FROM rec_proposal_cache_builds WHERE cache_id=NEW.cache_id AND state=0 "
           "AND written_chunks=NEW.chunk_index AND NEW.chunk_index<chunk_count AND "
           f"length(NEW.payload)=min({CHUNK_BYTES},byte_size-NEW.chunk_index*{CHUNK_BYTES})) "
           "BEGIN SELECT RAISE(ABORT,'Invalid recommendation cache prefix'); END")
    yield ("CREATE TRIGGER proposal_cache_chunk_cursor AFTER INSERT ON rec_proposal_cache_chunks BEGIN "
           "UPDATE rec_proposal_cache_builds SET written_chunks=written_chunks+1 WHERE cache_id=NEW.cache_id; END")
    yield ("CREATE TRIGGER proposal_cache_chunk_update BEFORE UPDATE ON rec_proposal_cache_chunks "
           "BEGIN SELECT RAISE(ABORT,'Recommendation cache chunks are immutable'); END")
    yield ("CREATE TRIGGER proposal_cache_chunk_delete BEFORE DELETE ON rec_proposal_cache_chunks "
           "WHEN NOT EXISTS(SELECT 1 FROM rec_proposal_cache_builds WHERE cache_id=OLD.cache_id AND state=2) "
           "BEGIN SELECT RAISE(ABORT,'Recommendation cache is still retained'); END")
    for operation in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER proposal_cache_head_{operation.lower()} BEFORE {operation} ON rec_proposal_cache_heads '
               'WHEN NOT EXISTS(SELECT 1 FROM rec_proposal_cache_builds WHERE cache_id=NEW.cache_id '
               'AND surface=NEW.surface AND state=1 AND written_chunks=chunk_count) '
               "BEGIN SELECT RAISE(ABORT,'Recommendation cache is incomplete'); END")
    # A direct legacy write is an explicit replacement, including callers that
    # invalidate caches with raw SQL. A root row also makes DELETE cascade to
    # the head, without reading or rewriting the old large payload at cutover.
    yield ("CREATE TRIGGER proposal_cache_legacy_update AFTER UPDATE OF payload ON rec_proposals BEGIN "
           "DELETE FROM rec_proposal_cache_heads WHERE surface=OLD.surface; END")


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)


def validate(conn):
    from .current import _tokens
    from .runner import MigrationError
    for sql in statements():
        name = sql.split()[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError(f'Incompatible recommendation cache structure: {name}')
