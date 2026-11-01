CREATE TABLE home_offer_dedup (
    id INTEGER PRIMARY KEY CHECK(id=1),
    epoch INTEGER NOT NULL CHECK(typeof(epoch)='integer' AND epoch>=0),
    built_at REAL NOT NULL CHECK(typeof(built_at) IN ('integer','real') AND built_at BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308)
);
CREATE TABLE home_offer_signatures (
    owner_id INTEGER NOT NULL REFERENCES home_offer_dedup(id) CHECK(owner_id=1),
    signature TEXT NOT NULL CHECK(typeof(signature)='text' AND length(CAST(signature AS BLOB)) BETWEEN 1 AND 1024),
    PRIMARY KEY(owner_id,signature)
) WITHOUT ROWID;
CREATE TRIGGER home_offer_signature_budget BEFORE INSERT ON home_offer_signatures
WHEN (SELECT count(*) FROM home_offer_signatures WHERE owner_id=NEW.owner_id)>=100
AND NOT EXISTS(SELECT 1 FROM home_offer_signatures WHERE owner_id=NEW.owner_id AND signature=NEW.signature)
BEGIN SELECT RAISE(ABORT,'Home offer deduplication exceeds its window budget'); END;
CREATE TRIGGER retired_home_offer_insert BEFORE INSERT ON rec_proposals WHEN NEW.surface='home_shown'
BEGIN SELECT RAISE(ABORT,'Home offer deduplication has a typed owner'); END;
CREATE TRIGGER retired_home_offer_update BEFORE UPDATE ON rec_proposals WHEN NEW.surface='home_shown'
BEGIN SELECT RAISE(ABORT,'Home offer deduplication has a typed owner'); END;
