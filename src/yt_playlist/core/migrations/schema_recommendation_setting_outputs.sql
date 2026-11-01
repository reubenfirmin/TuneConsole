CREATE TABLE radio_seed_output (
    id INTEGER PRIMARY KEY NOT NULL CHECK(id=1),
    surface TEXT NOT NULL DEFAULT 'radio_seed_offer' CHECK(surface='radio_seed_offer'),
    chunk_key TEXT NOT NULL DEFAULT '' CHECK(chunk_key=''),
    menu_id TEXT CHECK(menu_id IS NULL OR length(menu_id) BETWEEN 1 AND 4096),
    comparison_run_id INTEGER CHECK(comparison_run_id IS NULL OR (typeof(comparison_run_id)='integer' AND comparison_run_id>0)),
    run_ref INTEGER REFERENCES rec_model_selection_runs(id),
    run_resolution TEXT,
    filename TEXT NOT NULL,
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
    byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND 1048576),
    CHECK((comparison_run_id IS NULL AND run_ref IS NULL AND run_resolution IS NULL) OR
          (comparison_run_id IS NOT NULL AND ((run_ref IS NOT NULL AND run_ref=comparison_run_id AND run_resolution IS NULL) OR
           (run_ref IS NULL AND run_resolution IS NOT NULL AND run_resolution='missing_run'))))
);
CREATE TABLE radio_seed_subjects (
    offer_id INTEGER NOT NULL REFERENCES radio_seed_output(id),
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 999),
    video_id TEXT NOT NULL CHECK(length(video_id)>0),
    entry_id INTEGER REFERENCES catalog_entries(id),
    resolution_reason TEXT,
    PRIMARY KEY(offer_id,position),
    CHECK((entry_id IS NOT NULL AND resolution_reason IS NULL) OR
          (entry_id IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_entry'))
) WITHOUT ROWID;
CREATE TABLE radio_seed_policies (
    offer_id INTEGER NOT NULL REFERENCES radio_seed_output(id),
    raw_version TEXT NOT NULL,
    policy_version TEXT REFERENCES recommendation_policies(version),
    resolution_reason TEXT,
    PRIMARY KEY(offer_id,raw_version),
    CHECK((policy_version IS NOT NULL AND policy_version=raw_version AND resolution_reason IS NULL) OR
          (policy_version IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_policy'))
) WITHOUT ROWID;
CREATE INDEX ix_radio_seed_policies ON radio_seed_policies(policy_version);
CREATE TABLE model_autotune_output (
    id INTEGER PRIMARY KEY NOT NULL CHECK(id=1),
    surface TEXT NOT NULL DEFAULT 'rec_autotune_result' CHECK(surface='rec_autotune_result'),
    chunk_key TEXT NOT NULL DEFAULT '' CHECK(chunk_key=''),
    observed_at REAL CHECK(observed_at IS NULL OR (typeof(observed_at) IN ('integer','real') AND abs(observed_at)<=1.7976931348623157e308)),
    filename TEXT NOT NULL,
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
    byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND 1048576)
);
