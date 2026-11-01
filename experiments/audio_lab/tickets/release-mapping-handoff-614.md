The bounded #614 experiment is complete. **Retain the production scorer and preserve #577's exact Jamendo aggression head as the research candidate.** The new blend and source-balanced model do not improve that candidate's ranking. This is an author recommendation for independent review, not acceptance or production integration.

Detailed report: `experiments/audio_lab/RELEASE_MAPPING_RESULTS.md`. Label crosswalk, rater support, usable-audio inventory and acquisition limits: `experiments/audio_lab/RELEASE_LABEL_AUDIT.md`. Prefit procedure: `experiments/audio_lab/RELEASE_MAPPING_PLAN.md`. These are retained workspace artifacts; no commit or push was made.

All primary methods use the same 470 Soundtracks anger ratings, grouped into 295 identities. Protected training pools are 187 Jamendo aggression tracks, 255 Emotify power excerpts and 467 Soundtracks anger excerpts; the target corpus is completely excluded in each transfer direction.

| Historical inputs | NDCG | Top-list lift | MSE |
| --- | ---: | ---: | ---: |
| Frozen shared scorer | .6217 | .2928 | .06476 |
| Exact #577 aggression head | **.7111** | **.3447** | .04118 |
| Newly selected aggression anchor | .6735 | .3137 | .03647 |
| Learned aggression/power blend | .5783 | .2543 | .03909 |
| Source-balanced native tasks | .6850 | .3202 | .03635 |

The blend selected 75% predicted power using nested Jamendo training MSE. Its browser NDCG drops from the exact head's .7112 to .5342: paired difference **-.1770 [-.2503, -.0240]**, with lift difference **-.1377 [-.1815, -.0180]**. Source balancing also remains below the exact head, while its apparent gain over the shared scorer remains uncertain. Neither new procedure passes the frozen +.02 NDCG / +.01 lift and positive paired-bound requirements. Lower MSE does not repair the top lists.

The newly selected anchor uses ridge alpha 100 rather than the prior head's 1000; both controls remain explicit. The blend also regresses against this new anchor. Reciprocal regression checks in the written report include the new-anchor comparison: on Emotify the blend loses .0242 NDCG and .0112 lift, exceeding the material point-loss limits. The automatic `decisions.json` flags use legacy anchors; this additional check reinforces retain.

Observed human attributes contain useful native information, but the same mapping loses accuracy when fed audio-predicted attributes: power MSE rises from .02022 to .02667 and anger MSE from .01964 to .02536, with paired intervals excluding zero. Direct audio heads have lower MSE than the predicted-attribute path for all three references. Ranking differences in that diagnostic are uncertain. No supplied source directly co-annotates aggression, power and anger or measures cathartic suitability.

The blend raises scores for high-energy/low-anger excerpts without improving the five high-anger excerpts with estimated tempo at most 90 BPM. Zero high-anger/low-energy examples and one gentle-happy example leave those contrasts unestablished. The 84-recording local snapshot has seven explicit Release positives, eleven partial captures and no new identities; it remains supplementary, with unselected vibes unknown.

The audit found 1,072 unused published-audio matches from 230 new Jamendo artist IDs. A future two-shard plan for 70 and 63 could add 41 tracks from 34 artists within a 1.25 GiB ceiling, subject to verification and a new freeze. That is a small native-aggression extension, not new anger-transfer confirmation. No audio was acquired in this round.

Validation completed:

- `./experiments/run.py release freeze`, `release fit`, `release evaluate`, `release verify`: completed on `local/release-614-20261030/round-2/`; all receipts verify.
- `./experiments/run.py check --test test_release_mapping.py --test test_release_results.py`: **11 passed in 1.49s**. Seven protocol checks plus four real-artifact checks cover nested exclusions, prefit membership, source balance, missing labels, checksums, independent metric recalculation and coefficient replay.
- `./experiments/run.py check --syntax-only`: 142 research Python files parsed.
- Nine scorer exports pass replay and resource limits; maximum replay error below 8e-16, largest 33,703 bytes, slowest measured warm three-excerpt call .1514 ms, process peak RSS 119,181,312 bytes. Measurements exclude encoder/application startup and were taken under uncontrolled concurrent host activity.
- Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0. Intervals use 1,000 paired whole-group draws on previously inspected corpora, conditional on fixed fits.

Plan SHA256: `f5c8eeb1dce146ff294a772e3cb787f56f78ce7065d1b0123a6fc83f430abfce`.
Evaluation receipt SHA256: `84081c0db2c25dc95d583e94c205bd8ad9d673eb2bfd4fa1e4ac3911bd99d7b8`.

Per the user's clarification, the test lock is only for the global suite. This is documented in `AGENTS.md` and research documentation. Release execution and focused research checks no longer use it. The unfitted `round-1` is preserved; `round-2` has identical input hashes, folds, candidates and acceptance criteria, with execution/check lock handling corrected. No global suite was run.

Handoff: **In review**, for independent code/data/acceptance assessment. No production model, live database, #405/#587 acceptance scope or #137 genre behavior was changed by this work. No new listening collection or shadow phase is required.
