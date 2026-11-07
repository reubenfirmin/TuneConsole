"""Retain exact evidence before a controller changes its interpretation."""
import json

from yt_playlist.core import legacy_journals
from yt_playlist.core.legacy_evidence_contract import DOMAINS, MAX_BYTES
from yt_playlist.core.metadata_contract import clock


class RetainedOriginals:
    def __init__(self, repository, files):
        self.repository, self.files = repository, files

    def retain(self, domain, kind, source, payload, reason, *, now=None):
        if (domain not in DOMAINS or not all(isinstance(value, str) for value in (kind, source, payload, reason))
                or not kind or not reason or len(payload.encode()) > MAX_BYTES):
            raise ValueError('Invalid retained original')
        at = clock(now)
        try:
            body = json.loads(payload)
        except ValueError:
            body = None
        values = dict(body) if isinstance(body, dict) else {}
        if domain == 'artist' and kind == 'ambiguous_album_artist':
            values['artist_id'] = source
        with self.files.retained(), self.repository.transaction():
            inputs = self.repository.inputs(domain, kind, source)
            if inputs['reference'] is not None:
                self.files.read_receipt(domain, inputs['reference'])
                return
            publication = reference = None
            identity = legacy_journals.key(kind, source)
            if inputs['layout'] == 'journal' and identity in self.files.journal_entries(domain, inputs['publication'], [identity]):
                return
            subjects = self.repository.subject_ids(domain, values)
            if inputs['layout'] == 'journal':
                retention, basis = legacy_journals.disposition(domain, kind, payload, reason)
                publication = self.files.publish_journal(domain, inputs['publication'], {identity: {
                    'payload': payload, 'reason': reason, 'archived_at': at, 'subjects': subjects,
                    'state': 'retained', 'retention': retention, 'disposition': basis}})
            elif inputs['layout'] == 'receipt':
                reference, = self.files.publish_receipts(domain, [payload])
            self.repository.store(domain, kind, source, payload, reason, inputs, subjects,
                                  publication=publication, reference=reference)
