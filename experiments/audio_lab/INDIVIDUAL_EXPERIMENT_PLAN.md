# Remaining individual-vibe experiments: #577–#580

30 October 2026, before fitting. This is one computational campaign with four
separate decisions. Release is first; Energize, Relax and Uplift keep their own
native references and acceptance criteria. No production files or live data
are changed. Existing negative broad searches are retained.

## Hypothesis and bounded candidates

The new hypothesis is that independent native conditional-mean heads with a
small ridge-only search reduce selection variance and calibration drift from
the earlier 59-candidate mapped-ranking search. This narrows the earlier
native-MSE diagnostic too: no feature-family selection, nearest neighbors or
RFF, and no target-ranked source choice. For each native column, compare a
source mean and ridge alpha 0.1, 1, 10, 100, 1000 by source-only grouped MSE.
Keep embedding, acoustic and combined procedures separate. Freeze all five
outer and three inner folds, input bytes, identities, code, runtime and mapping
before fitting. Mean and median constants remain controls.

Sources are PMEmo, DEAM, Emotify, Soundtracks, explicit-human Jamendo,
Coimbra's tag-derived classes, excerpt-mismatched EMMA, and CAL500 activities.
Preserve original grouped outer folds for native OOF. The native-MAE OOF and
original mapped selector are exact historical controls on the identical native
rows. New local identities are excluded before any training.

For transfer, exclude the union of local identities, every other source and
all primary target cohorts before source-only selection. Fit each protected
source once. Close artist/recording/audio components, including explicit
Jamendo artist/track IDs and repeated recording-window IDs. Declared film and
prompt groups constrain folds but are not cross-corpus artist identities.
Unknown aliases and backbone pretraining remain unresolved.

## References and comparisons

Release: Emotify power and Jamendo aggressive, with Soundtracks anger as the
main common reference. Add reciprocal transfers among these three sources;
Coimbra aggressive and EMMA power are weaker supplementary controls. Do not
use VA, tempo or energy as Release truth. The fixed equal Emotify/Jamendo
embedding mean is a separate declared ensemble, never target-weighted.

Energize: PMEmo/DEAM arousal, Emotify mean(power, joyful activation),
Soundtracks energy; CAL500 Exercising is separate direct activity evidence.
Relax: mean(Emotify calmness, inverse tension), mean(inverse VA arousal,
valence), mean(Soundtracks inverse energy, inverse tension, tenderness).
Uplift: VA valence, Emotify joyful activation and mean(Soundtracks happy,
valence). These are suitability references, not causal mood-change outcomes.
Jamendo relaxed/happy and CAL500 sleep stay separate native/proxy tasks.
Previously completed MERGE quadrant evidence is retained without refitting.

Primary target cohorts are ManyMusic natural/generated separately, Memo2496,
and Soundtracks. Include exact frozen/refreshed shared outputs, historical and
browser preparation, source means, and the named prior Emotify combined ridge
and PMEmo embedding ridge. Keep source identity filtering visible for reused
full-source controls. Compare every eligible method on the same rows within a
reference. Shared normalized score error has different semantics from native
rating error; neither chooses the winner by itself.

PMEmo/DEAM embedding predictions have a predeclared equal-weight ensemble for
each supported vibe and preparation. Add both reciprocal VA directions.
Primary pilot caches contain embeddings only. Acoustic/combined predictions
are therefore evaluated on native and Soundtracks data, not fabricated for
pilot/browser inputs. Browser embeddings come from #424's exact pinned case
receipts. Mean native predictions over all available rated chunks precedes
the fixed mapping; first/three/all chunks are retained as diagnostics.

## Evidence, limits and decision

Report native MSE/MAE separately, plus mapped tie-aware NDCG, top-set mean/lift,
MSE, five fixed calibration bins and every fold. On primary cohorts use 1,000
paired whole-group bootstrap draws within fixed folds; publish intervals
against the exact checkpoint, refreshed controls and source mean. These are
conditional diagnostic intervals on already inspected corpora, without a
multiple-search correction. No post-result source, blend or hyperparameter
change is permitted in this round.

Frozen annotation slices use thresholds <=0.35 and >=0.65 on normalized
native labels: anger versus energy; happy low-energy versus unhappy
high-energy; calm versus sadness/tension. Predicted acoustic tempo <=90 BPM
is explicitly an unvalidated feature-defined slice, never human tempo truth.
Empty slices remain unknown. Memo's released chunk annotations support changes
over time; ManyMusic's whole-window annotations do not provide chunk truth.
Current local positive labels and existing one/three-excerpt captures supply
supplementary consistency only. Unselected vibes remain unknown.

Serialize all protected source heads before evaluation; independently replay
each native head. Measure exported per-vibe size, JSON load, 100 warm
three-excerpt calls and process peak RSS. Bounds remain 256 KiB per single
scorer, 512 KiB for a declared two-source ensemble, 100 ms warm inference,
1 GiB process RSS; encoder/application startup is outside this Python check.
Keep all candidates and failures regardless of the outcome.

Recommend adoption only with coherent primary-domain ranking/top-list gains,
no material regression, replay/resource success and credible confirmation.
Otherwise retain the incumbent with an explicit no-go per vibe. Native-only
gains, sparse local positives and a selected good domain cannot justify a
replacement. No user listening packet or shadow rollout is required.

For new Jamendo exports the publisher's current repository states metadata
CC BY-NC-SA 4.0, individual audio CC terms and noncommercial research/academic
use; other uses require Jamendo authorization. The source is
<https://github.com/MTG/mtg-jamendo-dataset#license>, inspected 30 October 2026.
Publisher commit: `cafd8e20c265ed84f1e61f1c875327971f43a62f`.
This new research artifact does not inherit acceptance of any other artifact's
terms. Its receipt will retain the source/data hashes and research-only status.

Commands: `./experiments/run.py individual freeze`, `individual fit`,
`individual evaluate`, `individual verify`. Use a new output directory if any
frozen input/code changes. The normal bounded machine lock owns fitting,
evaluation and focused tests; no concurrent heavy validation is launched.
