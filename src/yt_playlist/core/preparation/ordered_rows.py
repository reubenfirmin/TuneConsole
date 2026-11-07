"""Checkpoint individual members of one source document at a time.

The definition owns source capture and schema validation. Document planning and
durable file I/O run outside SQLite transactions; only the selected member slice
and its checkpoint enter the writer transaction. No runtime owner is selected.
"""
from dataclasses import dataclass
import json
import time

from ..schema_contract import MigrationError


@dataclass(frozen=True)
class OrderedJob:
    definition: object
    prepare: object
    begin: object
    copy: object
    complete: object
    delete: object
    revision_aware: bool = False
    resume_read: object | None = None
    repair_files: object | None = None
    copy_batch: object | None = None

    @property
    def name(self):
        return self.definition.name

    @property
    def state(self):
        return self.definition.state

    def install(self, conn, **kwargs):
        return self.definition.install(conn, **kwargs)

    def progress(self, conn):
        return self.definition.progress(conn)

    def step(self, conn, *, batch_size=128, retry=False, clock=time.time):
        if type(batch_size) is not int or not 1 <= batch_size <= 512:
            raise ValueError('Preparation batch size must be from 1 through 512')
        if len(self.definition.keys) != 1:
            raise ValueError('Ordered document preparation requires one source owner')
        if conn.in_transaction:
            raise MigrationError('Preparation requires an idle connection')
        current = None
        try:
            conn.execute('BEGIN')
            self.definition._version(conn)
            self.definition.validate(conn)
            current = self.progress(conn)
            if current is None:
                raise MigrationError('Preparation is not initialized')
            if current.status == 'failed' and not retry:
                raise MigrationError('Repair the input and retry explicitly: ' + current.error)
            if retry and self.repair_files is not None:
                conn.rollback()
                self.repair_files(conn,current)
                conn.execute('BEGIN')
                if self.progress(conn) != current:
                    return self.progress(conn)
            if current.status == 'prepared':
                return current
            highwaters = self.definition.scan_highwaters(conn, current)
            cursor = json.loads(current.cursor) if current.cursor else None
            if isinstance(cursor, dict):
                source, key = next(iter(self.definition.keys)), cursor['key']
                start = cursor['offset'] if cursor['generation'] == current.input_revision else 0
                read = self.resume_read if start and not retry and self.resume_read is not None else self.definition._read
                row = read(conn, source, key)
                work = [(source, key, dict(row) if row is not None else None)]
                next_owner, checkpoint = current.owner, key if current.owner else None
            else:
                next_owner, checkpoint, work = self.definition.select(conn, current, 1, highwaters=highwaters)
                start = 0
            conn.rollback()
            source, key, row = work[0] if work else (None, None, None)
            plan = (self.prepare(conn, row, revision=current.input_revision, offset=start,
                                 rebuild=retry and current.status == 'failed') if self.revision_aware
                    else self.prepare(conn, row)) if row is not None else None
            total = len(plan.units) if plan is not None else 0
            end = min(start + batch_size, total)
            # A durable plan can page its units from verified files. Load the
            # selected slice before taking the writer reservation.
            selected = [(position, plan.units[position]) for position in range(start, end)]
            generation = current.input_revision
            conn.execute('BEGIN IMMEDIATE')
            if self.progress(conn) != current:
                return self.progress(conn)
            self.definition.save_highwaters(conn, highwaters)
            if row is not None:
                if start == 0:
                    self.begin(conn, row, generation, plan)
                if self.copy_batch is not None:
                    self.copy_batch(conn, row, generation, selected)
                else:
                    for position, unit in selected:
                        self.copy(conn, row, generation, position, unit)
                if end == total:
                    self.complete(conn, row, generation, plan)
            elif key is not None:
                self.delete(conn, source, key)
            if key is not None:
                if end == total:
                    conn.execute(f'DELETE FROM {self.definition.changes} WHERE owner=? AND source_key=?', (source, key))
                else:
                    checkpoint = json.dumps({'key': key, 'generation': generation, 'offset': end})
            complete = (next_owner is None and end == total
                        and not conn.execute(f'SELECT 1 FROM {self.definition.changes} LIMIT 1').fetchone())
            conn.execute(f'UPDATE {self.state} SET status=?,owner=?,cursor=?,processed=processed+?,error=NULL,'
                         'prepared_revision=CASE WHEN ? THEN input_revision END,updated_at=? WHERE id=1',
                ('prepared' if complete else 'copying' if next_owner else 'reconciling', next_owner, checkpoint,
                 end-start, int(complete), clock()))
            conn.commit()
            return self.progress(conn)
        except Exception as exc:
            conn.rollback()
            if current is not None and not (current.status == 'failed' and not retry):
                conn.execute('BEGIN IMMEDIATE')
                if self.progress(conn) == current:
                    conn.execute(f"UPDATE {self.state} SET status='failed',error=?,prepared_revision=NULL,updated_at=? WHERE id=1",
                                 (str(exc)[:2000], clock()))
                conn.commit()
            raise
        finally:
            conn.rollback()
