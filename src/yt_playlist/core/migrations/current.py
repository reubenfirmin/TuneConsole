"""Release-baseline definition and current installed-layout validation."""
from ..schema_contract import table_clauses as _table_clauses
from .schema_definition import (
    CONTRACT_FLOOR, VERSION, DEFINITIONS,
    CURRENT_DEFINITION, CURRENT_SEEDS, SEED_SETTINGS,
    MigrationError, statements, contract, bootstrap as _bootstrap, validate,
    _tokens, _checks,
    _foreign_key_deferrals, _table, _schema_contract, _vibe_score_contract,
    _validate, _validate_data,
)


def bootstrap(conn, registry, now):
    return _bootstrap(conn, registry, now, validation=validate)
