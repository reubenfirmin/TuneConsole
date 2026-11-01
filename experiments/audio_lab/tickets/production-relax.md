Parent: #530. Priority: **P2**. Research: #617 and #579.

**In review.** Implementation and author checks are complete. Evidence is in
`experiments/audio_lab/RELAX_PRODUCTIONIZATION.md`: 168 focused checks, exact
coefficient replay, the 84-song capture cohort, bounded rollout/rollback, and
159 default-mode installed-wheel cases. Independent acceptance remains open.

The user selected #617's fixed Relax consensus and per-excerpt prediction
averaging, consistent with Cruise and Release. Use the frozen full-fit model
SHA256 `46ca8cc085adaab92eed16fa51b49ac4de311996a8b9492f144a5cf70aec7851`.
Its blend is 50% Emotify, 25% Soundtracks, 12.5% ManyMusic (equal natural and
generated experts), and 12.5% Memo. The learned-weight alternative failed the
predeclared transfer limits and must not be substituted.

## Implementation and acceptance

- Package the exact eleven native ridge heads and preprocessing statistics;
  do not refit. Predict and clip each excerpt, average native attributes, then
  apply the frozen linear mappings and fixed weights. Record the serving
  aggregation separately from the original mean-embedding training procedure.
- Default to active independent Relax suitability. Preserve Cruise, Release,
  other vibes, shared classification and shared radio intensity. Scores are
  uncalibrated suitability within Relax, not comparable six-way probabilities.
- Include artifact and routing identity in bounded background preparation.
  Replace each recording's contribution, preserve immutable history and human
  feedback, and propagate from original measurements with one recording vote.
- Preserve useful fallbacks for incomplete captures, missing metadata,
  incompatible embeddings and missing or malformed artifacts. Support rollback
  through `YT_PLAYLIST_AUDIO_VIBE_RELAX_MODE=off`, without recapturing audio.
- Verify independent coefficient replay, the 84-song frozen capture cohort,
  first/partial/complete capture behavior, bounded rollout and exact rollback,
  consumer score selection, and default inference from an isolated built wheel.
  Runtime must not read experiment files or annotations.
- Preserve source attribution and the existing source-terms status; this export
  does not establish distribution clearance for the derived artifact.

Reuse #617's grouped held-out and leave-corpus-out research. Those previously
inspected populations do not establish new independent confirmation, and several
gain intervals include zero. The aggregation-only diagnostic reused the same
held-out heads and found negligible ranking changes; it was not a new fit.

Reuse #424 capture acceptance and #573's broader full-account/rendered consumer
review. #405/#587 own whole-output evaluation. Existing consumer failures remain
tracked by #624. The implementation handoff does not close those dependencies.

Experiments and focused checks do not require the global-suite test lock.
Leave changes uncommitted for review.
