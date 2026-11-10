"""Retained original bytes in installed journal and receipt formats."""
from contextlib import contextmanager
from pathlib import Path

from . import file_map, legacy_evidence, legacy_evidence_contract, policy_documents


class OriginalFiles:
    def __init__(self, journals_root, receipts_root):
        self.journals_root, self.receipts_root = Path(journals_root), Path(receipts_root)

    @contextmanager
    def retained(self):
        with policy_documents.retained_reader(self.journals_root), policy_documents.retained_reader(self.receipts_root):
            yield

    def journal_entries(self, domain, publication, keys):
        return file_map.get_many(self.journals_root / domain, publication, keys)

    def publish_journal(self, domain, previous, values):
        return file_map.update(self.journals_root / domain, previous, values)

    def read_receipt(self, domain, reference):
        return legacy_evidence_contract.read(domain, *reference)[1]

    def publish_receipts(self, domain, payloads):
        return list(legacy_evidence._publish(self.receipts_root / domain, domain,
            (legacy_evidence._encode(domain, payload) for payload in payloads)))
