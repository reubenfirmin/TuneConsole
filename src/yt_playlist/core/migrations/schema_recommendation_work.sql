CREATE TABLE model_selection_schedule (
    id INTEGER PRIMARY KEY CHECK(id=1),
    next_at REAL NOT NULL CHECK(typeof(next_at) IN ('integer','real') AND next_at BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308),
    updated_at REAL NOT NULL CHECK(typeof(updated_at) IN ('integer','real') AND updated_at BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308)
);
CREATE TABLE tv_preparation_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    failed INTEGER NOT NULL CHECK(typeof(failed)='integer' AND failed IN (0,1)),
    observed_at REAL CHECK(observed_at IS NULL OR (typeof(observed_at) IN ('integer','real') AND observed_at BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308)),
    updated_at REAL NOT NULL CHECK(typeof(updated_at) IN ('integer','real') AND updated_at BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308)
);
CREATE TRIGGER retired_recommendation_work_insert BEFORE INSERT ON rec_proposals
WHEN NEW.surface IN ('daily_model_schedule','tv_preparation')
BEGIN SELECT RAISE(ABORT,'Recommendation work has a typed owner'); END;
CREATE TRIGGER retired_recommendation_work_update BEFORE UPDATE ON rec_proposals
WHEN NEW.surface IN ('daily_model_schedule','tv_preparation')
BEGIN SELECT RAISE(ABORT,'Recommendation work has a typed owner'); END;
