"""Bind a rebuild's captured SQL receipts to its database-free evidence reader."""
from yt_playlist.core.evidence_ledger import directory
from yt_playlist.core.evidence_record_files import EvidenceRecordFiles
from yt_playlist.repos.evidence_file_writes import references


def evidence_view(conn):
    return EvidenceRecordFiles(directory(conn), 2**63 - 1, references=references(conn))
