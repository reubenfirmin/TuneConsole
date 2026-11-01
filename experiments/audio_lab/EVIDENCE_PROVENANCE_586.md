# Musical evidence and listening intent (#586)

The audit reused the existing observations, normalized vibe history, prepared
profiles and listening evidence ledger. No schema or migration was added.
#498/#499 describe later architecture work and remain outside this change.

## Existing behavior verified

- Track details distinguish measurements, provider predictions, legacy metadata,
  genre/inherited hints, explicit corrections, unknown values and conflicting
  sources. Raw provider outputs retain units, model/provider versions and lookup
  methods. A newer source cache is never attributed to an older resolved profile.
- `musical_character.compact` retains the profile version/digest and the stored
  vibe inputs version. On Air retains the actual temporary tuning, ordering
  policy, musical policy, track-level intensity source/support and cached profile
  identifiers with its frozen options and context.
- Mix Tapes records the actual recipe and complete offered tracks. Responses
  refer to an exact immutable opportunity and option; account, queue and upload
  matching constrain subsequent player outcomes. A later menu or model does not
  rewrite that choice. Automatic continuation adds experience to the same episode.
- Temporary mix intent expires separately from lasting taste. Generated-play
  quarantine survives playlist deletion and distinguishes an independent user
  play or explicit adoption from a generated recommendation returning as input.
- Propagated predictions refer to original `measurements` through
  `prediction_inputs`. Original audio measurements refer to `sample_attributes`
  through `measurement_samples`. Cohort fitting uses one original measurement
  per distinct recording/vibe; predictions, repeated plays, alternate uploads
  and development annotations do not become new independent measurements.

## Missing behavior added

- Track details now include six collapsed **Vibe estimates**, showing audio,
  provider attribute, artist, genre, or artist-and-genre origin. Inherited values
  show the number of other recordings. Scores are explicitly estimates rather
  than user ratings; a neutral fallback without usable evidence stays Unknown.
  Stored prediction policy and model inputs versions are available in a nested
  disclosure. Rendering only reads prepared data; it performs no inference.
- Final Mix Tapes recipes now explicitly retain the requested vibe, including no
  request, plus musical and vibe scoring policy versions. Those versions also
  participate in the prepared menu key so a policy change invalidates an old menu.
- Ordinary mix journey ordering previously read cached musical profiles but
  dropped them from its returned tracks. Served track dictionaries now retain
  those exact compact profiles and their intensity provenance. An already
  prepared track keeps its older profile; missing evidence remains missing.

Implementation: `web/musical_evidence.py`, its existing track partial,
`rec/home_cards.py` and `rec/home_prepared.py`. Existing radio, lineage and
quarantine implementations were audited rather than duplicated.

## Actual account evidence

Captured 2026-10-30 UTC, schema 81, using SQLite `mode=ro`, `query_only=ON` and one
pinned read transaction. No Store initialization, migrations, tests or simulated
listening actions ran against the account. The production repository's relational
overlay was used to reconstruct the same profiles served by the track route.

The snapshot contains 10,254 cached profiles, all with physical-profile version
`musical-profile-2/genre-priors-1/2026-10-20.2`. Counts below describe profiles
containing a source; categories can overlap and are not accuracy estimates.

| Saved source | Profiles | Pinned rendered example |
| --- | ---: | --- |
| Audio model | 124 | Danny Tenaglia — Don't Turn Your Back (Mendo Remix) [Mixed] |
| Provider attributes | 2,607 | delving — Vast |
| Artist and genre | 1,309 | Tycho — Ascension |
| Artist | 956 | Out Of Silence — Indian Spirit |
| Genre | 1,776 | X-Dream — The Second Room ( A Trip ) (Remaster 2002) |
| Attribute fallback | 1,801 | Khruangbin — Full Performance (Live on KEXP), unknown scores |
| Explicit correction | 3 | Kliment — Forest of Wishes |
| Conflicting sources | 3 | Mord Fustang — Better with Friends |

10,253 profiles lack at least one of the ten physical attributes; this does not
mean that all of their attributes or vibe estimates are missing. Current stored
vibe inputs include the preview/model digest and prediction policy
`measured-vibes-1/subgenres-only-1`. Old account results are displayed as stored;
the audit does not silently regenerate them using today's model.

The database retains 48,168 current vibe rows, 30,726 immutable measurements,
6,354 measurement/sample links, 51,693 historical predictions and 918,436 original
contributor links. Selected current examples retain their measurement/prediction
IDs, model/input versions, original contributor recordings and audio sample IDs.
These counts are an inventory, not a claim that every historical row was reviewed.

The existing account ledger contains 534 opportunities and 1,538 responses. The
snapshot retains the latest 24 of each, resolves response references and freezes
the corresponding context manifests. For example, historical Mix Tapes offer
`dc862e27d1993a726a08a1c0a7a92808` retained its musical version and ranker but lacks
an explicit vibe policy. This missing historical field remains unknown. Radio
offer `09c994f107bf4daabe48ec35f49f851c` retains tuning `.5/.5/.5`, ordering policy
`cluster-radio-v8-skip-blackouts` and the musical policy; its visible response
resolves to that same offer/context. Visibility is not a listening outcome.

## Validation and reproduction

```sh
./experiments/run.py evidence-audit snapshot
./experiments/run.py evidence-audit check
./experiments/run.py evidence-audit browser
```

Ignored artifacts are under `experiments/audio_lab/local/evidence-586/`:
`snapshot.json` pins current source hashes, actual profiles and representative
ledger/lineage records; `browser-390.json` and `browser-1280.json` pin snapshot and
screenshot hashes plus Python/Chromium versions. Screenshots use the production
partial, description code and styles with those real saved profiles. These are
component renders, not evidence that the running installed app loaded this source.
A separate full-app browser journey exercises the isolated test server.

Focused tests cover normalized serving, corrections, sparse metadata, source-cache
changes, exact frozen Mix Tapes profiles/outcomes, policy changes, temporary-intent
expiry, radio provenance, generated-play quarantine, distinct contributor lineage,
partial audio runs, retractions, model-version recomputation and annotation exclusion.
Controlled score and playback fixtures are used for state transitions; actual
account data supplies the representative source/version inventory and UI cases.
No human listening or paired judgments are required. Independent review owns final
acceptance and full-suite integration checks.

Observed author checks on Python 3.14.8:

- Research syntax: 128 Python files parsed successfully.
- Focused product run: 59 passed and one failed in 292.26 seconds. The failure
  selected a historical offer by file/list order after changing the policy. The
  test now resolves the exact offer ID returned in the revised page.
- Follow-up on the final track/journey paths: three pure description cases passed;
  26 cases could not initialize fresh test databases after concurrent schema work
  advanced the bootstrap to version 100 (`no such table: main.play_events`).
  This is a setup error before the affected product assertions, not a passing
  result. Logs retain the full commands and traces.
- Real-account component browser checks: two passed in 29.78 seconds on Chromium
  148.0.7778.96. Nine actual source/correction/conflict/sparse cases were exercised
  at each of 390 and 1280 pixels. Keyboard disclosures, source text and horizontal
  fit passed. Eighteen screenshots were saved; audio, inheritance, corrections,
  conflicts and all-unknown examples were also inspected visually.

Outstanding independent acceptance: rerun the final focused checks and isolated
full-app browser journey after the shared fresh-schema bootstrap is working.
Component screenshots do not replace that full-app check. Nothing was committed
or pushed, and no full suite was run by this implementation agent.
