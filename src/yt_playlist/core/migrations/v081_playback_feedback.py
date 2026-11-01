"""Receipts atomically acknowledge independently retained playback feedback."""
from pathlib import Path


def statements():
    yield Path(__file__).with_name('schema_playback_feedback.sql').read_text()


def apply(conn, context):
    # Receipt installation can now advance independently of bulk v80 work.
    # Keep the historical version stamp truthful: its actual owner must exist
    # with the exact installed contract before the legacy chain records v81.
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='playback_feedback_receipts'").fetchone():
        validate(conn)
    else:
        for sql in statements():
            conn.execute(sql)


def validate(conn):
    from .regular import validate_receipts
    validate_receipts(conn)
