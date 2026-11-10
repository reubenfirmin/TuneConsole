CREATE TABLE playback_feedback_receipts (
    capture_id TEXT PRIMARY KEY NOT NULL,
    account_id INTEGER NOT NULL REFERENCES identities(id),
    kind TEXT NOT NULL CHECK(kind IN ('rating','player','live')),
    observed_at REAL NOT NULL CHECK(typeof(observed_at) IN ('integer','real') AND
                                  observed_at BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308),
    result INTEGER NOT NULL CHECK(typeof(result)='integer' AND result IN (0,1))
);
