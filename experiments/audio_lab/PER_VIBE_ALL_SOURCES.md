# Per-vibe specialists: extension to all thirteen sources

The user requested that the fresh comparison cover all datasets and confirmed
that each of the six vibes must be evaluated independently. This extends the
first batch in `PER_VIBE_ROUND.md`; its results remain unchanged. It is a new
frozen batch, not an expansion of the completed batch's reported search.

Run the same native target selection on all thirteen frozen expert sources:
Soundtracks, Emotify, original-vote CAL500, PMEmo, VGMIDI, Jamendo, DEAM,
Coimbra MIREX-like, MERGE, matched EMMA previews, MERP, matched AMG1608 and
matched NTWICM. These are thirteen distinct source versions, not every historical
variant of each corpus. Overlapping historical variants and the new-corpus
transfer pilots retain their existing separate roles.

Reuse the four completed native stages only when the actual input arrays,
groups, target order, source hashes, grid, seed and fitting code match. Copy their
checksummed outputs with an explicit receipt identifying the first batch. Fit
the other nine sources. Preserve partial and failed stages.

Keep the same ridge, 64-feature random-Fourier ridge and neighbor grid and native
MAE selection. Native per-target MAE and correlations remain the external human
evidence. The original annotations, supplied-audio or matched-audio status,
excerpt mismatch and missing-artist limits remain attached to each source.
MAE comparisons between different native tasks are not valid. Small native
samples and sparse activity labels can favor uninformative constant predictions;
retain the training-median control.

For the local diagnostic use raw embeddings, the incumbent native representation,
all thirteen frozen experts and all thirteen target-specialist representations.
Run all five predeclared selectors again, choosing separately for every vibe in
the independent selectors. Do not change grids or the conservative threshold
after the first batch's negative result. Keep the shared selector and incumbent
controls, all fold choices, all per-vibe metrics and serialization verification.

Historical 55 labels remain exploratory; the 20 inspected reserved additions are
excluded. All 76 local identities protect new native fitting. More search on
these 55 labels does not create independent evidence. No live model change or
per-vibe winner chosen after outer results is authorized by these reports.

Use `./experiments/run.py all-specialists plan`, then `all-specialists run
--independent-execution`. `plan` records source code, arrays, grids and reuse
provenance before fitting. The isolated execution flag uses the user's existing
authorization; normal pytest retains its machine lock.
