Historical planning note, updated for the user's direct-mainlining instruction:
Cruise is now integrated into production and active by default. #574 is closed
as not planned. The five other vibes retain the validated shared model.

The next round now has explicit subtasks for all six vibes, plus the shared
evaluation and deployment work. All are P2. Research implementation remains
under `experiments/` through `./experiments/run.py`.

| Vibe | Subtask | Starting point and next decision |
| --- | --- | --- |
| Cruise | #571, evaluated with #570 | Package the fixed equal PMEmo/DEAM candidate; compare with a refreshed baseline and advance first if calibration and operational checks pass. |
| Focus | #576 | Retain direct Reading/Studying supervision and prior native heads; obtain a stronger confirmation comparison rather than choose an inspected search winner. |
| Release | #577 | Compare Emotify power and Jamendo human aggression against independent anger/aggression references; keep activation separate and resolve release permissions through #424. |
| Energize | #578 | Test embedding/combined activation experts with PMEmo, DEAM, Emotify, Soundtracks and CAL500 exercise; the Cruise ensemble and acoustic-only preference do not establish an upgrade. |
| Relax | #579 | Compare calmness/tension, low-arousal positive VA and direct relaxed-vote experts against both product baselines on shared audio-backed references. |
| Uplift | #580 | Compare valence/joy/happiness experts on shared references, including gentle positive music; distinguish Uplift from activation. |

Shared work:

- #570: freeze the enlarged local corpus, reserve new groups and compare a
  refreshed version of the current model. The latest read-only inventory is
  **84 usable labeled recordings**, eight identities beyond the prior 76-ID
  protected snapshot; the shipped artifact still reports 55 local training songs.
- #571: compact, strictly validated per-vibe artifact and replay contract,
  starting with Cruise and reusable by the other model tasks.
- #572: independent suitability scores, calibration, abstention and consumer
  thresholds. Replacing one vibe must not renormalize the other five.
- #573: bounded rescoring, propagation and startup checks on an isolated real
  library copy, reusing compatible captures and preserving fallback/rollback.
- #574: cancelled. There is no Cruise shadow phase or staged enablement.
  Ordinary integration verification stays in #573; existing listening and
  resource investigations stay in #405/#406.

Every model task identifies its external annotation references, controls,
identity exclusions, calibration/resource checks and acceptance evidence. Use
external human annotations as the main reference, supplied audio first, with
local labels supplementary and stored predictions never promoted to gold.
Do not compare raw scores across different reference datasets or assume a
shared source, learner or set of thresholds is best for all vibes.

The exact configured-product comparison and shipping plan are documented in
`experiments/audio_lab/PRODUCT_CANDIDATE_PLAN.md`. Cruise improves external NDCG
on all three audited pilots; Focus/Release gains are smaller and Energize does
not show a consistent replacement benefit. Relax/Uplift still need the direct
product audit now explicitly tracked above. These are exploratory reused-corpus
results, not completed listening or production acceptance.

Starting with #570; #571 and the five vibe model investigations are Ready. The
later shared integration/acceptance tasks remain Backlog until their inputs are
established. #530's completed evidence remains In review for independent review.
