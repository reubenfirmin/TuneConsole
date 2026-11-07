-- Current metadata delivery ownership. No historical payload reconstruction.
ALTER TABLE artist_credit_observations ADD COLUMN metadata_delivery_id INTEGER REFERENCES metadata_deliveries(id);
CREATE TABLE metadata_deliveries (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 provider TEXT NOT NULL CHECK(length(provider)>0),
 key_contract TEXT NOT NULL CHECK(key_contract IN ('provider_revision','capture','legacy_record')),
 delivery_key TEXT NOT NULL CHECK(length(delivery_key)>0),
 provider_entity_type TEXT NOT NULL CHECK(provider_entity_type IN ('recording','release','artist','genre','item','lookup','legacy_unknown')),
 provider_entity_id TEXT CHECK(provider_entity_id IS NULL OR length(provider_entity_id)>0),
 input_entry_id INTEGER REFERENCES catalog_entries(id),
 input_recording_id INTEGER REFERENCES recordings(id),
 input_video_id TEXT REFERENCES youtube_items(video_id),
 observed_at REAL CHECK(observed_at IS NULL OR (typeof(observed_at) IN ('integer','real') AND observed_at BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308)),
 contract_digest TEXT NOT NULL UNIQUE CHECK(length(contract_digest)=64 AND contract_digest NOT GLOB '*[^0-9a-f]*'),
 raw_filename TEXT CHECK(raw_filename IS NULL OR length(raw_filename)>0),
 raw_digest TEXT CHECK(raw_digest IS NULL OR (length(raw_digest)=64 AND raw_digest NOT GLOB '*[^0-9a-f]*')),
 raw_byte_size INTEGER CHECK(raw_byte_size IS NULL OR (typeof(raw_byte_size)='integer' AND raw_byte_size BETWEEN 1 AND 8388608)),
 raw_offset INTEGER CHECK(raw_offset IS NULL OR (typeof(raw_offset)='integer' AND raw_offset>=0)),
 format_version TEXT NOT NULL CHECK(format_version='metadata-delivery-1'),
 CHECK(provider_entity_id IS NOT NULL OR provider_entity_type IN ('lookup','legacy_unknown')),
 CHECK(observed_at IS NOT NULL OR key_contract='legacy_record'),
 CHECK((input_entry_id IS NULL AND input_recording_id IS NULL AND input_video_id IS NULL) OR (input_entry_id IS NOT NULL AND input_recording_id IS NOT NULL)),
 CHECK((raw_filename IS NULL AND raw_digest IS NULL AND raw_byte_size IS NULL AND raw_offset IS NULL) OR (raw_filename IS NOT NULL AND raw_digest IS NOT NULL AND raw_byte_size IS NOT NULL AND raw_offset IS NOT NULL))
);
CREATE UNIQUE INDEX ix_metadata_delivery_scope_key ON metadata_deliveries(provider,key_contract,delivery_key,provider_entity_type,coalesce(provider_entity_id,''),input_entry_id IS NULL,coalesce(input_entry_id,0));
CREATE INDEX ix_metadata_delivery_raw_content ON metadata_deliveries(raw_digest,id) WHERE raw_digest IS NOT NULL;
CREATE INDEX ix_metadata_delivery_raw_file ON metadata_deliveries(raw_filename) WHERE raw_filename IS NOT NULL;
CREATE INDEX ix_metadata_delivery_input ON metadata_deliveries(input_entry_id,id) WHERE input_entry_id IS NOT NULL;
CREATE INDEX ix_metadata_delivery_recording ON metadata_deliveries(input_recording_id,id) WHERE input_recording_id IS NOT NULL;
CREATE INDEX ix_metadata_delivery_video ON metadata_deliveries(input_video_id,id) WHERE input_video_id IS NOT NULL;
CREATE INDEX ix_metadata_delivery_entity ON metadata_deliveries(provider,provider_entity_type,provider_entity_id,id);
CREATE TRIGGER immutable_metadata_delivery BEFORE UPDATE OF id,provider,key_contract,delivery_key,provider_entity_type,provider_entity_id,input_entry_id,input_recording_id,input_video_id,observed_at,contract_digest,raw_digest,raw_byte_size,format_version ON metadata_deliveries BEGIN SELECT RAISE(ABORT,'Metadata delivery identity and evidence are immutable'); END;
CREATE TABLE metadata_capture_heads (entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),provider TEXT NOT NULL,capture_slot TEXT NOT NULL CHECK(length(capture_slot)>0),delivery_id INTEGER NOT NULL REFERENCES metadata_deliveries(id),PRIMARY KEY(entry_id,provider,capture_slot)) WITHOUT ROWID;
CREATE INDEX ix_metadata_capture_delivery ON metadata_capture_heads(delivery_id);
CREATE TRIGGER owned_metadata_capture_insert BEFORE INSERT ON metadata_capture_heads WHEN NOT EXISTS(SELECT 1 FROM metadata_deliveries d WHERE d.id=NEW.delivery_id AND d.input_entry_id=NEW.entry_id AND d.provider=NEW.provider) BEGIN SELECT RAISE(ABORT,'Metadata capture head has a different input or provider'); END;
CREATE TRIGGER owned_metadata_capture_update BEFORE UPDATE ON metadata_capture_heads WHEN NOT EXISTS(SELECT 1 FROM metadata_deliveries d WHERE d.id=NEW.delivery_id AND d.input_entry_id=NEW.entry_id AND d.provider=NEW.provider) BEGIN SELECT RAISE(ABORT,'Metadata capture head has a different input or provider'); END;
CREATE INDEX ix_artist_credit_metadata_delivery ON artist_credit_observations(metadata_delivery_id) WHERE metadata_delivery_id IS NOT NULL;
CREATE TRIGGER immutable_credit_metadata_delivery BEFORE UPDATE OF metadata_delivery_id ON artist_credit_observations BEGIN SELECT RAISE(ABORT,'Artist capture delivery is immutable'); END;
CREATE TRIGGER owned_credit_metadata_delivery BEFORE INSERT ON artist_credit_observations WHEN NEW.metadata_delivery_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM metadata_deliveries d WHERE d.id=NEW.metadata_delivery_id AND d.provider=NEW.source AND d.input_entry_id=NEW.entry_id AND NEW.capture_kind='provider_delivery' AND NEW.delivery_key='metadata-delivery:'||d.id) BEGIN SELECT RAISE(ABORT,'Artist credit evidence has a different metadata delivery owner'); END;
