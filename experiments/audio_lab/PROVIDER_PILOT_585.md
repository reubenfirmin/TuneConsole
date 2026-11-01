# Bounded metadata provider pilot (#585)

Measured 2026-10-30 UTC against the account database opened with SQLite
`mode=ro`, `query_only=ON`, and one pinned read transaction. No migrations,
provider ingestion, audio uploads, accounts, or production writes occurred.

**Recommendation: do not adopt either provider from this evidence.** ReccoBeats
returned useful-looking candidates, but added zero verified recording/field
coverage. GetSongBPM is unavailable without an application key and attribution
setting; neither was configured. That is a completed prerequisite assessment,
not a GetSongBPM coverage measurement.

## Frozen sample and matching

Artifacts: `local/provider-585/{plan,sample,report}.json`, `fetch.log`, and
`reccobeats/{requests.json,cache/*.json}`. These account-specific artifacts are
ignored by Git. The report includes hashes of every retained response.

- Sample SHA256: `d9e27aae40de501f621cd94de9e7631116efc796fdd58a1b2d4f63aaf2f9b0c6`.
- Executed tool SHA256: `8788f95d80647062bed98fc0d3c240fe15a54e7358446fbcabc4e4e5c1668cfa`.
- Source schema version: 81; 13,960 eligible distinct recordings.
- Twelve deterministic recordings: two per library/discovery and
  conflicting/missing/retained evidence cell. Recording IDs, upload IDs, titles,
  artists, release metadata, durations, native identifiers and their acceptance
  status, and retained numeric observations are pinned per input.
- Conflicting means an unresolved metadata conflict of any field, or a legacy
  slow/high-energy or fast/low-energy discrepancy. Missing means both BPM and
  energy are absent. Those cells are sampling aids, not correctness labels.
- Alternate uploads are collapsed before sampling, preferring a known duration.
  Sampling preserves missing durations; this is not a duration-known-only sample.
  The strata are deliberately balanced and cannot estimate catalog-wide coverage.

Verification requires literal normalized title (preserving live/remix/edit/
remaster qualifiers), artist, duration within the larger of 3 seconds or 2%, and
agreement on an **accepted** shared ISRC or Spotify recording identifier. A
candidate-status catalog ID is not accepted identity. Multiple viable results
remain ambiguous. Native MusicBrainz IDs cannot be compared directly with
ReccoBeats' own track IDs. Five search results per input limit search recall.

| Membership / evidence | Input | Decision and material evidence |
| --- | --- | --- |
| Library / conflicting | Deafheaven — Dream House | Ambiguous: title/artist/555-second duration pass; no accepted shared ID. Live and anniversary remix/remaster rejected. Retained candidate ISRC actually refers to the rejected remaster. |
| Library / conflicting | Smashing Pumpkins — Muzzle | No acceptable match: only explicitly remastered titles returned; local duration absent. |
| Library / missing | Finger Fest Inc. — Auto Porno | Ambiguous: title/artist/544-second duration pass; no accepted shared ID. Four named alternate mixes rejected. |
| Library / missing | Pet Shop Boys — It's a Sin (Disco Mix) (Disco Mix; 2018 Remaster) | No search results; duration absent. |
| Library / retained | Markus Schulz — Bells Of Planaxis | Rejected duration mismatch: local 288 seconds; returned ISRC also differs from the local candidate ISRC. |
| Library / retained | Luke Slater — Origin | Ambiguous: title/artist/195-second duration and candidate ISRC agree, but that local identifier remains candidate-status. |
| Discovery / conflicting | Nibana — I Am a Soul | Ambiguous: exact title/artist, duration absent. |
| Discovery / conflicting | Mat Zo — Order out of Chaos | Ambiguous: exact title/artist and candidate ISRC agreement; duration absent. |
| Discovery / missing | Kieran Hebden, Steve Reid — Arrival | No search results; duration absent. |
| Discovery / missing | Union Jack — Red Herring (Ainur Remix) (feat. Ainur) | No search results; duration absent. |
| Discovery / retained | James Holden — A Break In The Clouds (Main Mix) | No search results; duration absent. |
| Discovery / retained | Death In Vegas — Leather | Ambiguous: multiple exact-title candidates, duration absent. |

## Coverage, disagreement, and cost

- Twelve inputs completed; six ambiguous and six without an acceptable match.
- Nineteen returned candidates: eight title/version rejections, one duration
  rejection, three lacking an accepted shared ID, seven lacking local duration.
- **Verified recording additions: 0/12. Incremental verified fields: 0.** Matching
  precision/error rate is unknown without independently established identities.
  Rejections are policy decisions, not nine demonstrated provider errors.
- Seventeen HTTP requests: twelve searches and five candidate feature reads.
  All returned 200. Payload total 20,883 bytes; HTTP wall time 15.18 seconds, plus
  spacing at 1.25 seconds minimum. Twelve repeated searches hit local cache and
  generated zero additional requests. Budget was 36 requests; 429 stops retries.
- Five feature payloads remain diagnostic candidate data. None count as adopted
  coverage or evaluation truth. For Dream House the candidate tempo is 82.211
  versus retained 164.535 BPM, a likely half/double-time disagreement; this does
  not establish which estimate is right. Origin has near-identical tempo
  (127.059 vs 127.033), but danceability differs (.525 vs .9998). Order out of
  Chaos has near-identical tempo (125.824 vs 125.831), but danceability differs
  (.170 vs .9766) and instrumentalness differs (.0299 vs .7741).
- Full per-observation deltas are retained. Repeated deliveries of the same
  value are not independent corroboration. Cross-provider classifier outputs
  do not become human labels, calibrated probabilities, or interchangeable
  attribute definitions because they share a field name or numeric range.

A narrower future pilot could start with recordings whose accepted ISRC/Spotify
identity and upload duration already agree. Origin is a concrete candidate for
automated identity reconciliation. Do not broaden name-only ingestion or defer
the audio-model work for these providers. No human listening is required.

## Current primary-source contracts

Checked 2026-10-30 UTC:

- [ReccoBeats terms](https://reccobeats.com/docs/documentation/terms-of-service),
  updated May 25, 2026: keyless, free personal/commercial API; foundational track
  metadata originates with Spotify, analytical features are the provider's own;
  third-party obligations still apply. No accuracy or availability guarantee.
- [Rate limits](https://reccobeats.com/docs/documentation/rate-limiting): internal
  limits, HTTP 429 and Retry-After, caching recommended. No fixed numerical quota
  was promised. The pilot stopped well below its own request cap.
- [Track resource](https://reccobeats.com/docs/documentation/Resources/track):
  duration in milliseconds, optional ISRC, own track ID, Spotify URL. Artist IDs
  and album product codes are not recording identity.
- [Feature definitions](https://reccobeats.com/docs/documentation/Analysis/audio-features-extraction):
  tempo in BPM; loudness in dB; energy, danceability, instrumentalness,
  acousticness, liveness, speechiness and valence in 0–1. These are provider
  estimates of different properties, not our vibe labels. The pilot fetched
  catalog features only, never called the upload/extraction endpoint. API docs
  identify specification 1.0.0; the responses do not identify an analysis-model
  revision, so that revision remains unknown.
- [GetSongBPM API](https://getsongbpm.com/api): registered application, valid email,
  key and an existing backlink required; free with attribution; 3,000 requests
  per hour. Header authentication avoids placing keys in URLs. Tempo is BPM;
  danceability/acousticness are 0–100 and documented as sourced from AcousticBrainz,
  so cannot independently corroborate our AcousticBrainz observations. The
  documented song/search contract has no duration or recording-level MusicBrainz
  ID; the documented MBID belongs to the artist. An account/key alone would not
  solve recording verification. No GetSongBPM request was made.

## Reproduction

```sh
./experiments/run.py provider-pilot freeze --output experiments/audio_lab/local/provider-585-new
./experiments/run.py provider-pilot fetch --output experiments/audio_lab/local/provider-585-new
./experiments/run.py check --test test_provider_pilot.py
```

Fetching the original plan reuses cached responses and verifies sample/tool hashes.
Freezing a new plan reads the current catalog and may select different recordings.
The configured GetSongBPM branch remains unexercised against the real service.
