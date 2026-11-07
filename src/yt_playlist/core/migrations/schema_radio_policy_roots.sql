CREATE TABLE radio_session_policies (
    session_id TEXT NOT NULL REFERENCES radio_sessions(id) ON DELETE CASCADE,
    raw_version TEXT NOT NULL,
    policy_version TEXT REFERENCES recommendation_policies(version),
    resolution_reason TEXT,
    PRIMARY KEY(session_id,raw_version),
    CHECK((policy_version IS NOT NULL AND policy_version=raw_version AND resolution_reason IS NULL) OR
          (policy_version IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_policy'))
) WITHOUT ROWID;
CREATE INDEX ix_radio_session_policies ON radio_session_policies(policy_version);
CREATE TABLE radio_seed_policies (
    setting_key TEXT NOT NULL REFERENCES settings(key) ON DELETE CASCADE CHECK(setting_key='radio_seed_offer'),
    raw_version TEXT NOT NULL,
    policy_version TEXT REFERENCES recommendation_policies(version),
    resolution_reason TEXT,
    PRIMARY KEY(setting_key,raw_version),
    CHECK((policy_version IS NOT NULL AND policy_version=raw_version AND resolution_reason IS NULL) OR
          (policy_version IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_policy'))
) WITHOUT ROWID;
CREATE INDEX ix_radio_seed_policies ON radio_seed_policies(policy_version);
