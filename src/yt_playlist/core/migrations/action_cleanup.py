"""Fold draft state and playlist leases into their existing action owners."""
from functools import lru_cache

from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'fold_action_storage'
TABLES = ('action_drafts', 'action_placement_intents', 'action_placement_items',
          'action_placement_handles', 'action_plan_resolutions', 'action_backup_writes',
          'action_target_claims')
DRAFT_GUARDS = ('action_draft_owner_insert', 'action_draft_owner_update')
REMOVED = frozenset((*TABLES, *DRAFT_GUARDS))
REVIEW_GUARDS = ('action_reviewed_draft_insert', 'action_reviewed_draft_update')
DRAFT_COLUMNS = (
    "draft_signature TEXT CHECK(draft_signature IS NULL OR kind='merge_draft')",
    'draft_account_id INTEGER REFERENCES accounts(id)',
    'draft_updated_at REAL CHECK(draft_updated_at IS NULL OR '
    "(typeof(draft_updated_at) IN ('integer','real') AND abs(draft_updated_at)<=1.7976931348623157e308))",
)
CLAIM_COLUMNS = (
    'claim_token TEXT CHECK(claim_token IS NULL OR '
    '(length(claim_token)=32 AND account_id IS NOT NULL AND playlist_scope_id IS NOT NULL))',
    'claim_until REAL CHECK((claim_token IS NULL AND claim_until IS NULL) OR '
    "(claim_token IS NOT NULL AND typeof(claim_until) IN ('integer','real') "
    'AND abs(claim_until)<=1.7976931348623157e308))',
)
DRAFT_INDEX = 'CREATE UNIQUE INDEX uq_actions_draft_signature ON actions(draft_signature) WHERE draft_signature IS NOT NULL'
CLAIM_INDEX = ('CREATE UNIQUE INDEX uq_action_targets_claim ON action_targets(account_id,playlist_scope_id) '
               'WHERE claim_token IS NOT NULL')
TARGET_GUARD = ('CREATE TRIGGER action_target_immutable BEFORE UPDATE OF '
                'id,action_id,position,role,account_id,unresolved_account_id,playlist_scope_id,'
                'provider_playlist_id_snapshot,membership_ref_id,unresolved_membership_ref_id,resolution_reason '
                "ON action_targets BEGIN SELECT RAISE(ABORT,'Committed action targets are immutable'); END")


def _review_guard(sql):
    return sql.replace('FROM action_drafts d', 'FROM actions d').replace('d.signature=', 'd.draft_signature=')


def install(conn, _context):
    for table, columns in (('actions', DRAFT_COLUMNS), ('action_targets', CLAIM_COLUMNS)):
        for column in columns:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {column}')
    for name, sql in definitions(conn, REVIEW_GUARDS).items():
        conn.execute('DROP TRIGGER ' + name)
        conn.execute(_review_guard(sql))
    conn.execute('''
        UPDATE actions SET
            (draft_signature,draft_account_id,draft_updated_at,revision,
             recovery_filename,recovery_digest,recovery_bytes) =
            (SELECT signature,account_id,updated_at,revision,recovery_filename,recovery_digest,recovery_bytes
             FROM action_drafts WHERE id=actions.id)
        WHERE id IN (SELECT id FROM action_drafts)
    ''')
    if conn.execute("SELECT 1 FROM actions WHERE kind='merge_draft' AND draft_signature IS NULL LIMIT 1").fetchone():
        raise MigrationError('A saved merge draft has no selection')
    conn.execute('DROP TRIGGER action_target_immutable')
    conn.execute(TARGET_GUARD)
    conn.execute('''
        UPDATE action_targets SET (claim_token,claim_until)=
            (SELECT claim_token,lease_until FROM action_target_claims WHERE action_target_id=action_targets.id)
        WHERE id IN (SELECT action_target_id FROM action_target_claims)
    ''')
    conn.execute(DRAFT_INDEX)
    conn.execute(CLAIM_INDEX)
    for table in (*reversed(TABLES[1:4]), TABLES[0], *TABLES[4:]):
        conn.execute('DROP TABLE ' + table)


def validate(conn):
    names = definitions(conn, (*REMOVED, 'uq_actions_draft_signature', 'uq_action_targets_claim'))
    if REMOVED.intersection(names) or not {'uq_actions_draft_signature', 'uq_action_targets_claim'} <= names.keys():
        raise MigrationError('Action storage was not folded')


@lru_cache(maxsize=4)
def contract(*, compact, memberships_folded):
    """Describe the mutable action schema on either side of this change."""
    from .schema_definition import _schema_contract, statements

    owners = ('actions', 'action_targets')
    names = (*REVIEW_GUARDS, 'action_target_immutable')
    ddl = []
    for sql in statements():
        words = sql.split(maxsplit=3)
        if len(words) < 4:
            continue
        kind, name = words[1:3]
        if kind == 'TABLE' and name in (*owners, *TABLES):
            if name == 'action_targets' and memberships_folded:
                sql = sql.replace('REFERENCES playlist_membership_refs(id)', 'REFERENCES playlists(id)')
            if compact:
                if name not in owners:
                    continue
                columns = DRAFT_COLUMNS if name == 'actions' else CLAIM_COLUMNS
                first_constraint = '\n CHECK(' if name == 'actions' else '\n UNIQUE('
                sql = sql.replace(first_constraint, '\n ' + ',\n '.join(columns) + ',' + first_constraint, 1)
            ddl.append(sql)
        elif kind == 'TRIGGER' and name in (*names, *DRAFT_GUARDS):
            if compact:
                if name in DRAFT_GUARDS:
                    continue
                sql = TARGET_GUARD if name == 'action_target_immutable' else _review_guard(sql)
            ddl.append(sql)
    if compact:
        ddl.extend((DRAFT_INDEX, CLAIM_INDEX))
    return _schema_contract(ddl)
