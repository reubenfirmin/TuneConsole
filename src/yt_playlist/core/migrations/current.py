"""Historical import boundary for the installed schema definition."""
from ..schema_definition import (
    CONTRACT_FLOOR, VERSION, DEFINITION, DEFINITIONS, SOURCE_DEFINITIONS,
    CURRENT_DEFINITION, CURRENT_SEEDS, SEED_SETTINGS,
    MigrationError, migration_definitions, statements, contract, bootstrap as _bootstrap, validate,
    _migration_version, _statements, _tokens, _checks, _table_clauses,
    _foreign_key_deferrals, _table, _schema_contract, _normalized_vibe_contract,
    _validate, _validate_data,
)


def bootstrap(conn, registry, now):
    return _bootstrap(conn, registry, now, validation=validate)
