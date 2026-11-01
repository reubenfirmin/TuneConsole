Parent: #530. Follows #579. Priority: **P2**. **In review**. Use #530's shared research and label-mapping protocol. Owns the next Relax mapping investigation only. #579 owns the original comparison and supplies retained controls.

## Completed comparison and recommendation

The bounded consensus experiment is complete. Recommend the **fixed blend** for
review and possible productionization: 50% Emotify, 25% Soundtracks, 12.5%
ManyMusic (natural/generated experts equally), 12.5% Memo. The learned-weight
procedure fails the predeclared transfer regression limits. The user has selected
the fixed blend for production integration under #629, with per-excerpt clipped
prediction averaging to match Cruise and Release. Research acceptance remains
separate; the same-head aggregation diagnostic found negligible ranking changes.

On identical new joint grouped folds, previous Emotify embedding versus fixed
blend NDCG is **0.7782 / 0.8409 Soundtracks**, **0.7890 / 0.8111 ManyMusic natural**,
**0.8229 / 0.8687 generated**, and **0.8932 / 0.9258 Memo**. The equal-corpus mean
gain is +0.04310 against that control and +0.02887 against the refitted anchor.
Browser comparisons preserve the pattern. Fixed-blend omitted-corpus comparisons
stay within the declared limits; most transfer intervals still include zero.
The changed fold grouping means these exact-control summary scores differ from
#579's old tables. References and retained predictions did not change.

Author evidence: `experiments/audio_lab/RELAX_CONSENSUS_RESULTS.md` and the
retained run below. Eight focused checks passed, including independent numerical
replay of every outer/transfer prediction. The 181,507-byte research export
passes replay, inference and memory bounds. No global suite/test lock was used.
Independent acceptance remains outstanding; previously inspected corpora are
diagnostics, and the historical combined-model pilot comparison is still missing.

## Question and evidence

Can calmness and low tension provide a reliable Relax mapping, with low activation as supporting evidence rather than a replacement for calmness?

The 30 October Emotify candidate improved point estimates on natural/generated ManyMusic and Memo, but their paired intervals included no gain. Its Soundtracks gain was clearer: NDCG +0.2159 [0.1174, 0.3013]. The prior Emotify combined head remained stronger there (0.8170 versus new combined 0.8036). The equal low-arousal/valence ensemble failed Soundtracks, and reciprocal VA Relax transfer was below random. This makes calmness/tension supervision the useful starting point.

Evidence: `experiments/audio_lab/INDIVIDUAL_EXPERIMENT_RESULTS.md`, frozen `local/individual-20261030/remaining-1/`, plan SHA256 `c7c69cb680a7c6a7919b35984356b295521a53271d66cf48270677af230829ae`. These are retained workspace artifacts; independent acceptance remains pending.

## Strategy

1. Inventory Emotify calmness/tension, Soundtracks energy/tension/tenderness, Jamendo relaxed votes and VA annotations. Preserve sleep and tag-derived low-positive quadrants as separate proxies. Document whether quiet/tense and calm/sad distinctions have actual human support.
2. On existing co-annotated excerpts, measure whether observed low arousal, tenderness or valence helps explain a separately observed calmness/tension reference. Keep the reference's defining columns out of mapping inputs. Compare that diagnostic with cross-fitted audio predictions to separate a semantic mismatch from poor prediction.
3. The user authorized a consensus comparison across Soundtracks, Memo and ManyMusic on 30 October. Freeze three procedures: the Emotify calmness/inverse-tension anchor, a fixed blend with source-native experts, and a learned nonnegative blend retaining at least half its weight on the anchor. Fit weights using corpus-balanced ranking loss and fully cross-fitted native predictions inside grouped training folds. Include the anchor-only zero-auxiliary control. Keep each source's reference unchanged; VA is a proxy and has no direct calmness/tension co-annotation. Sadness alone must not disqualify calm music, and low energy alone must not excuse tension. Do not require happiness.
4. Resolve the prior combined-model comparison gap by preparing the exact compatible acoustic features from already supplied pilot audio, if reproducible under the original feature policy. Pin new caches and retain browser/historical identities separately. If compatibility cannot be established, report the missing comparison explicitly.
5. Compare natural/generated music, Memo and Soundtracks separately, including changing arrangements and supported low-energy/tense cases. Use existing coverage first; additional supplied audio must address a documented contrast or uncertainty gap. Reused corpora remain diagnostics.

The executable protocol is `experiments/audio_lab/RELAX_CONSENSUS_PLAN.md`.
Refresh the enlarged local labeled set before freezing; the current read-only
export contains 84 usable songs, 15 Relax positives, and three additional labeled
songs without usable captures. Local annotations remain supplementary and
unselected vibes remain unknown. The external fit uses 400 Emotify recordings,
470 Soundtracks excerpts, 188 natural and 95 generated ManyMusic recordings,
and 63 usable Memo recordings. Evaluate both grouped outer folds and transfer
with an entire corpus omitted (both ManyMusic strata together). These inspected
populations do not become fresh confirmation after resplitting.

Frozen run: `experiments/audio_lab/local/relax-617-20261030/round-1/`;
plan SHA256 `c37bc4c30e8d42ae34d50a7f3aedebf898e61f61060dd01bc2caf2648abe3cf2`.

## Acceptance

- Show which native labels add information beyond calmness/tension, with observed-label and predicted-label diagnostics on fixed references.
- Compare the declared mappings with #579's exact shared/refreshed/browser controls, the latest Emotify candidate and the named prior combined head on every compatible cohort. Report ranking, top-list lift, error/calibration, paired uncertainty and missing comparisons.
- Decide whether the pilot gains persist without weakening the calm-versus-tense distinction or sacrificing a domain. Use predeclared meaningful-gain/regression limits and give an explicit adopt/retain recommendation.
- Replay/resource-check any proposed export. All work stays under `experiments/` through `./experiments/run.py`; production/output acceptance remains separate. No new listening collection or shadow phase.
