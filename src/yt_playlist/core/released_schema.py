"""Read-only structural admission for the supported v0.1.18 database."""
import hashlib
import json

from .schema_contract import MigrationError

VERSION = 64
DDL = '95036d09b6a69976b6b6898bd94d1eef88b6b670b089198c9092491f00bfa8b7'


def validate(conn):
    objects = [tuple(row) for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
        "WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name")]
    digest = hashlib.sha256(json.dumps(objects, separators=(',', ':')).encode()).hexdigest()
    if digest != DDL:
        raise MigrationError('Schema 64 does not match the supported v0.1.18 release')
