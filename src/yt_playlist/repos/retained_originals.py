"""SQL owners for retained originals in supported installed layouts."""
from contextlib import contextmanager

from yt_playlist.core import legacy_journals
from yt_playlist.core.legacy_evidence_contract import DOMAINS, SUBJECTS, FORMAT
from yt_playlist.repos.base import Repo, synchronized


class RetainedOriginalsRepo(Repo):
    @contextmanager
    def transaction(self):
        with self._lock, self.conn.atomic(join=True):
            self.conn.execute('UPDATE artists SET name=name WHERE 0')
            yield

    @synchronized
    def inputs(self, domain, kind, source):
        table = DOMAINS[domain]
        journal = legacy_journals.available(self.conn)
        receipts = self.conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (table,)).fetchone() is not None
        row = self.conn.execute(f'SELECT filename,content_digest,byte_size,file_offset FROM {table} '
            'WHERE kind=? AND source_id=?', (kind, source)).fetchone() if receipts else None
        return {'layout': 'journal' if journal else 'receipt' if receipts else 'inline',
                'publication': legacy_journals.head(self.conn, domain) if journal else None,
                'reference': tuple(row) if row else None}

    @synchronized
    def subject_ids(self, domain, values):
        result = {}
        for column, owner in SUBJECTS[domain].items():
            if values.get(column) is None:
                continue
            table, field = owner[:-1].split('(')
            if table == 'playlist_scopes' and not any(row[1] == 'ref' for row in self.conn.execute('PRAGMA table_info(playlist_scopes)')):
                field = 'id'
            row = self.conn.execute(f'SELECT {field} FROM {table} WHERE {field}=?', (values[column],)).fetchone()
            if row:
                result[column] = row[0]
        return result

    @synchronized
    def store(self, domain, kind, source, payload, reason, inputs, subjects, *, publication=None, reference=None):
        if inputs['layout'] == 'inline':
            self.conn.execute('INSERT OR IGNORE INTO identity_state_archive VALUES (?,?,?,?)', (kind, source, payload, reason))
        elif inputs['layout'] == 'journal':
            self.conn.execute('INSERT INTO legacy_archive_heads VALUES (?,?) ON CONFLICT(domain) '
                'DO UPDATE SET publication=excluded.publication', (domain, bytes.fromhex(publication)))
        else:
            columns = ''.join(',' + column for column in subjects)
            placeholders = ',?' * len(subjects)
            self.conn.execute(f'INSERT INTO {DOMAINS[domain]}(kind,source_id,reason,filename,content_digest,byte_size,'
                f'file_offset,format_version,resolution_status{columns}) VALUES (?,?,?,?,?,?,?,?,?{placeholders})',
                (kind, source, reason, *reference, FORMAT, 'resolved' if subjects else 'unresolved', *subjects.values()))
