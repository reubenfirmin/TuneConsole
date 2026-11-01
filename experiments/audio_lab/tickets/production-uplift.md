Parent: #530. Priority: **P2**. Research: #618 and #580. **In review.**

The user explicitly selected #618's fixed Uplift consensus after reviewing the
production comparison and accepting the generated-music tradeoff. Preserve the
original research retain decision: this is an explicit product decision despite
the strict nonregression rule, not a retroactive passing experiment.

Use the exact frozen full fit SHA256
`a1f1087b75f844eef74be62b39d627df611be9c2edabdc99920cbb591342f737`
and fixed weights: Soundtracks 25%; DEAM, PMEmo, Jamendo human, Emotify,
ManyMusic and Memo 12.5% each. Split ManyMusic equally between natural and
generated experts. Do not export the learned-weight alternative or refit.

Browser-input research NDCG versus the production baseline is Soundtracks
0.647 to 0.715, natural ManyMusic 0.705 to 0.762, generated ManyMusic 0.913 to
0.891, and Memo 0.890 to 0.973. These are inspected populations, with 63 Memo
records; NDCG is not classification accuracy. Preserve all reported uncertainty.

## Completed implementation handoff, 30 October 2026

Default active Uplift suitability now composes with Cruise, Release and Relax.
The exact artifact is 140,485 bytes, SHA256
`b29217c7f706c9a5eb0716c7cd8d175ec4a85e65f309806ef66351710e7c97af`.
Report, commands, hashes, runtime and review boundaries:
`experiments/audio_lab/UPLIFT_PRODUCTIONIZATION.md`.

- Independent arithmetic replay: 4,814 external preparation cases and 159
  first/partial/full capture cases; maximum error `3.3306690738754696e-16`.
- Focused integration tests: **197 passed**. No global suite or test lock.
- Fresh isolated replay of 84 actual captured songs: 73 complete recordings
  receive Uplift, 11 partial recordings retain fallback. Rollout 2.694 seconds
  of work; exact rollback 2.422 seconds. Other fits/intensity/history preserved.
- Built wheel: 159 cases, all four default specialists, exact score agreement,
  19 matching production files, no network or research imports.

These are author checks, not independent acceptance. The user's live database
was not modified. Changes remain uncommitted; the running app was not reloaded
or deployed.

## Implementation and acceptance

- Package exact native coefficients, preprocessing, fixed weights and provenance.
  Predict/clip each excerpt before averaging and mapping, matching Cruise,
  Release and Relax. Record mean-embedding training separately.
- Default to independent Uplift suitability. Compose with the other specialists;
  retain shared classification, radio intensity and remaining vibe scores.
- Include mode, artifact hash and routing version in bounded preparation.
  Replace recording contributions without duplicating votes; preserve immutable
  history, human feedback and propagation from original evidence.
- Retain useful fallbacks for incomplete captures, missing metadata, incompatible
  embeddings and malformed/missing optional artifacts. Provide local rollback
  with `YT_PLAYLIST_AUDIO_VIBE_UPLIFT_MODE=off` without new audio capture.
- Verify independent native/blend replay, real first/partial/full captures,
  fresh isolated preparation, continuing updates, exact rollback and consumer
  score selection. Check defaults in an isolated installed wheel; production
  inference must not read research files or annotations.
- Retain attribution and unresolved source-terms status. This integration does
  not establish distribution clearance.

Reuse #424 capture acceptance and #573's wider account/rendered consumer review;
#624 owns existing broader consumer defects and #405/#587 own output evaluation.
No new listening or shadow phase. Experiments and focused checks do not require
the global-suite lock. Leave changes uncommitted for review.
