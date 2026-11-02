# Vibe calculation storage (#692)

The seven score, sample and evidence owners keep their existing meanings. All
original measurements, measurement/sample links and raw samples remain durable;
superseding a prediction does not make its original inputs disposable.

The explicit isolated rebuild normalizes only three version columns:

```sql
CREATE TABLE vibe_versions (
    id INTEGER PRIMARY KEY,
    value TEXT NOT NULL UNIQUE
);
-- Other fields, checks, keys and relationships stay on their existing owners.
-- measurements.model_version becomes:
model_version_id INTEGER NOT NULL REFERENCES vibe_versions(id)
-- predictions.policy_version becomes:
policy_version_id INTEGER NOT NULL REFERENCES vibe_versions(id)
-- recording_vibes.inputs_version becomes:
inputs_version_id INTEGER NOT NULL REFERENCES vibe_versions(id)
```

The measurement uniqueness key includes `model_version_id`. Dictionary values
are immutable. Readers and API boundaries resolve IDs to the exact original text;
equal scores never make different versions equivalent. Current-row comparison,
prediction reuse and input invalidation continue to compare exact versions.

A regular change creates the empty dictionary and its guard only. Populated
installed tables retain their text columns and continuing writes. Conversion is
part of the separately resumable rebuild: initialize empty normalized owners,
intern versions while copying bounded batches, and compare decoded values and
types with every source row before installation. Applied history is preserved.
Backup uses SQL's foreign-key closure and the existing provenance file owner.

Superseded prediction summaries are disposable after 180 days from their known
`predicted_at`. This keeps six months for calculation investigations, longer
than the 90-day serving-report window, without accumulating every recalculation
indefinitely. Current predictions and unknown-age imported summaries remain.
No other durable SQL owner references a prediction outside its current result
and owned contributor/artist details; reports capture scores and configurations,
not live prediction IDs. Detailed contributor files keep #699's separate 30-day
retention. An expired detail is distinct from a missing required file. Inspecting
a retired calculation returns unavailable history, never invented empty inputs.

Idle collection scans a bounded primary-key page, checks current membership by
its recording/vibe key, and runs behind the provenance reader fence and SQLite
writer reservation. A persisted cursor resumes after restart. Pending work reads
current measured cohorts, which remain retained; guarded publication cannot reuse
a superseded cache row from a changed snapshot. The prediction allocation
high-water mark survives collection, so retired IDs cannot be reassigned.
