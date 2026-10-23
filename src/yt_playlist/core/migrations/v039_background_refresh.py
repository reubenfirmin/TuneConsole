"""Retire the replacement local models; retain coalesced source notifications.

Only derived local-learning state is removed. The established models, source
evidence, model-selection history and last completed serving caches survive.
"""
from .v034_progressive_learning import statements as learning_statements
from .v036_progressive_signals import statements as signal_statements

RETIRED_TABLES = (
    'rec_evidence', 'rec_local_nodes', 'rec_local_edges', 'rec_cluster_members',
    'rec_cluster_stats', 'rec_candidate_rows', 'rec_rank_residual',
    'rec_artist_contributions', 'rec_artist_stats', 'rec_projection_rows',
    'rec_projection_state', 'rec_challenger_nodes', 'rec_local_forecasts',
    'rec_source_members', 'rec_recent_signals', 'rec_learning_state',
)


def statements():
    for sql in (*learning_statements(), *signal_statements()):
        if not sql.startswith('CREATE TRIGGER '):
            continue
        header = sql.split(' BEGIN', 1)[0]
        name = header.split()[2]
        yield f'DROP TRIGGER {name}'
        yield (header + " BEGIN INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') "
               'ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1; END')
    for table in RETIRED_TABLES:
        yield f'DROP TABLE {table}'
    yield 'DELETE FROM rec_change_queue'
    yield "INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','')"
    yield ("DELETE FROM settings WHERE key IN ('rec_selection_active_run','rec_selection_restore') "
           "AND value='progressive'")
    yield ("DELETE FROM settings WHERE key='rec_content_model' AND CASE "
           "WHEN json_valid(value) THEN json_type(value,'$.hash_bins') IS NOT NULL ELSE 0 END")


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)
