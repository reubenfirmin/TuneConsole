"""Extend retained metadata findings with scoped, versioned source observations."""


def statements():
    yield 'ALTER TABLE metadata_findings ADD COLUMN provenance_json TEXT CHECK(provenance_json IS NULL OR json_valid(provenance_json))'
    yield 'ALTER TABLE metadata_findings ADD COLUMN observation_key TEXT'
    yield 'CREATE UNIQUE INDEX ix_metadata_observation_delivery ON metadata_findings(observation_key) WHERE observation_key IS NOT NULL'
    yield 'CREATE INDEX ix_metadata_observations_recording ON metadata_findings(recording_id,id) WHERE provenance_json IS NOT NULL'


def apply(conn, context):
    for statement in statements():
        conn.execute(statement)
