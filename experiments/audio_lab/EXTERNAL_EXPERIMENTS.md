# Offline external-data experiments

**Execution rule, clarified 30 October 2026:** the test lock is only for the
global suite. Experiments, dataset audits, fitting, research evaluation and
focused research checks run independently. This supersedes older coordination
instructions below. Use an existing independent-execution option where present;
remove obsolete experiment lock requirements rather than waiting for the suite.

Related issues: #530 (current research), #507 (earlier runs), #406, #405 and #136.
The later [#424 browser evaluation](BROWSER_EVALUATION_424.md) checks the current
packaged runtime; historical checkpoint runtime and results below stay separate.
All artifacts stay in `local/external-20261028/`, which is ignored by Git.
The #530 checkpoint exports the application database through read-only
transactions. It does not write application models or change the six vibes.

## Current #530 checkpoint

**Decision: retain the existing Emotify checkpoint.** Native source comparisons,
source/consensus ablations, the 346-recording new-audio transfer pilot and the
one-time reserved-label evaluation are complete. The examined replacements do
not establish better six-vibe suitability. Focus remains weak, and the primary
reserved slice has no positive Release examples. Derived intensity and model
agreement do not fill those human-evidence gaps.

The fixed actual-library campaign has attempted all five candidates. Four
completed; the activity-semantic candidate reached the fixed 30-second bound on
warm Radio retune 13. Its complete playlists are retained separately from the
uncompleted warm aggregate. A separate continuation reproduced all 27 ordered
playlists exactly and completed the missing restrictions with zero violations,
without repeating warm retunes or changing that failure.
The user authorized isolated library experiments to run without the shared test
lock and gave them priority. Pass `--independent-execution`; ordinary pytest checks
keep their existing locking. Attempt reports record host load because concurrent
work limits timing comparisons. The latest completed focused run passed 63
checks; two new metadata-boundary checks await the normal test lock.
Earlier replay attempts are partial: one reached
the 180-second publication bound; the extended checkpoint control produced
playlists before an intentional interruption, without a full warm aggregate or
restriction comparison. Fresh/cold preparation results below retain their own
coverage and time-bound limits. New dataset leads remain separate from these
completed comparisons and have not entered model selection.

The completion audit against #530's eight experiment steps is:

| Plan step | Retained evidence | Remaining limit or action |
| --- | --- | --- |
| Freeze external-first evaluation and local inputs | Pinned corpus identities, source groups, human snapshots, candidate/inference hashes and one-time reserved report | Keep frozen accuracy artifacts unchanged. Local results supplement native human evidence. |
| Targeted Emotify/CAL500 improvements | Early/center/late windows, listener weights, perceived versus induced emotion and original activity-vote comparisons | Focus remains weak; activity judgments do not establish improved concentration. |
| Finish ready audio-backed datasets | DEAM, EMMA, MERGE, matched AMG1608/NTWICM and MERP runs, with rejected joins/windows retained | Source overlap, preview alignment and research/release terms remain explicit. Unavailable audio is not a completion dependency. |
| Native learners, transfer and consensus | Native grouped comparisons, every nonempty compatible expert subset, source ablations and 346-recording independent transfer pilot | No target-tuned or post hoc fixed-subset winner. |
| Six suitability rankings | Per-vibe rankings, fixed semantic mappings and one-time reserved evaluation across all frozen candidates | No demonstrated replacement; primary reserved Release has zero positives, and unselected vibes are not unsuitable-intrusion judgments. |
| Local captures and stored predictions | Frozen lineage, zero-weight controls, local soft-teacher/neighborhood ablations and held-out checks | No reliable local-data gain; descendants and predictions add no independent human votes. |
| Unrated representations | Duplicate-free consistency, PCA/whitening and shared native-task comparisons | Retain negative results; playback duration and agreement are not labels. |
| Propagation and listening workflows | Withheld direct evidence, retraction/rebuild checks, browser parity, fresh/cold preparation, actual fresh consumers and all five fixed library candidates attempted; semantic restriction continuation complete | Retain the semantic retune failure. Full-account startup, human listening quality, provider pilots and release acceptance remain unestablished. |

Use the checkpoint runner for new heavy work. It acquires the same machine and
subprocess lifetime locks as pytest, runs one extraction worker, and records
input fingerprints and output checksums before reusing completed stages. The
older suite/campaign/follow-up commands below describe historical runs and do
not provide this test-lock coordination.

```sh
./experiments/run.py checkpoint --steps emotify-identities snapshot rater-weights cal500-ranking
./experiments/run.py checkpoint --steps local-pool emotify-plus merp deam emma emotify-windows
```

`./experiments/run.py` is the common executable entrypoint. Run it from the
repository root so Codex can match the reusable `./experiments/run.py` command
prefix. Each command accepts `--help`:

```sh
./experiments/run.py status
./experiments/run.py acquire manymusic --rated-audio
./experiments/run.py acquire memo2496 --audio-pilot 64
./experiments/run.py audit manymusic
./experiments/run.py audit manymusic-provenance
./experiments/run.py audit memo2496
./experiments/run.py windows plan
./experiments/run.py windows audit
./experiments/run.py --log audio_lab/local/external-20261028/checkpoint-1/new-corpora/campaign.log pilot --after-checkpoint
./experiments/run.py reference
./experiments/run.py check
./experiments/run.py evidence
./experiments/run.py complete --inspect
./experiments/run.py --log audio_lab/local/external-20261028/checkpoint-1/completion/campaign.log complete
./experiments/run.py local-export
./experiments/run.py reserved freeze
./experiments/run.py reserved evaluate
./experiments/run.py library inspect
./experiments/run.py library freeze-runtime
./experiments/run.py library startup --seconds 120
./experiments/run.py library fresh --seconds 120
./experiments/run.py library paired --seconds 0
./experiments/run.py capture
./experiments/run.py preprocessing --decode-only
./experiments/run.py preprocessing
./experiments/run.py comparisons
./experiments/run.py --log audio_lab/local/external-20261028/checkpoint-1/continuation.log checkpoint --keep-going --steps deam temporal-deam learners-deam
```

The launcher selects the checkout's existing Python environment and fixed research
modules. `check` uses the approved `uv run pytest` form and focused research
test files. It retains isolation fixtures while excluding the root global-suite
lock and worker orchestration. The historical focused run below passed 63
checks in 2.01 seconds, including reserved-artifact, serving-math,
consumer-adapter, resume, cleanup and payload-isolation controls. The actual-data
consumer campaign retains four complete candidates and one bounded failure. The
new focused run waited 60 seconds and exited on another agent's test lock; this
did not stop independent experiments. Protocol changes also receive
actual-data checks; syntax parsing alone is not acceptance.
`check --syntax-only` parses research Python without
acquiring the machine lock or claiming test coverage. `--log` appends both output streams within
`experiments/`, avoiding shell redirection around the approved command prefix.
`status` reads saved state and file counts; it does not prove process liveness,
validate feature caches or establish dataset acquisition integrity. Those checks
belong to the checkpoint and dataset commands.

`windows plan` freezes a bounded held-out pilot before extraction: one exact
thirty-second window per ManyMusic recording, chosen by identity hash without
using its targets or listener count, and Memo's complete 15–75-second interval.
The current plan has 188 natural recordings, 95 generated recordings and 63
eligible Memo recordings (one of the 64 acquired files has incomplete labels).
Its 1,227 ten-second chunks cover those rated intervals consecutively. Real
duration and decoder checks can reject further intervals; padding is forbidden.
This is a recording-balanced transfer pilot, not the complete new benchmarks.

`pilot --after-checkpoint` waits for this particular checkpoint invocation,
without holding the machine test lock, for at most eight hours. A separate
campaign-owner lock prevents duplicate pilot coordinators. It then runs the
focused checks, rebuilds the metadata overlap audit, extracts aligned chunks,
and evaluates source-only-selected PMEmo and DEAM ridge models. It stops on a
failed stage or changes to its code/frozen-plan identity. Each heavy child takes
the shared machine lock normally. State and logs live in `checkpoint-1/new-corpora/`.

Both prediction on the mean embedding and the mean of chunk predictions are
fixed before target evaluation. Source-mean and neutral-midpoint predictions
are controls. Report natural audio, generated audio and Memo separately, plus
all-audio and conservative overlap-excluded slices. Artist/prompt groups enter
the conditional paired bootstrap. No target fitting, local-label selection or
product export is part of this pilot. ManyMusic's source curation and Memo's
missing artist identities still limit independence.

The corrected pilot completed all 346 recordings and all 24 fixed transfer
directions on 29 October. An exact floating-point equality in the chunk-layout
guard had incorrectly rejected 21 complete natural-audio intervals. Regression
checks now accept binary rounding while rejecting microsecond gaps and genuinely
short audio. `windows repair` applies only to the identified faulty revision:
it archives the original 325-row results, validates unchanged input/runtime
hashes, reuses 1,164 verified chunks with explicit original-cache lineage, and
extracts the remaining 63 chunks. Original artifacts remain in
`checkpoint-1/new-corpora/archives/float-interval-1/`.

The final overlap-excluded slices contain 178 natural recordings (158 groups),
95 generated recordings (88 groups), and 63 Memo recordings (62 groups). Ten
natural recordings have known prior-corpus identity/artist overlap. Memo's
grouping cannot establish artist independence. MAE averages normalized valence
and arousal; the intervals are conditional paired group-bootstrap reductions
against each model's own source-mean control:

| Source model | Natural MAE | Generated MAE | Memo MAE |
| --- | ---: | ---: | ---: |
| PMEmo static | 0.16424 | 0.10173 | 0.10530 |
| PMEmo aligned | 0.17019 | 0.09600 | 0.10808 |
| DEAM static | 0.16777 | 0.11097 | 0.10901 |
| DEAM aligned | 0.17118 | 0.09901 | 0.07640 |

PMEmo static reduces natural MAE by 0.03379 (95% interval 0.02119–0.04651).
DEAM aligned reduces Memo MAE by 0.04943 (0.03401–0.06432). DEAM static's Memo
reduction is inconclusive: 0.00625 (−0.00986–0.02265). Every fixed method remains
reported; these comparisons do not select a target-tuned winner. Mean embedding
and mean chunk prediction agree to numerical precision for these fitted linear
models. Neither new corpus has entered training or local calibration.

`complete` runs the remaining comparisons sequentially under the shared machine
lock. It checks code, frozen identities, source manifests, actual feature files
and output hashes before reuse, archives replaced reports, and stops if pinned
inputs change during a stage. It adds duplicate-free consistency, further native
learners/classification, PCA/whitening targets, temporal/perceived/multiwindow
alternatives, all 16,383 subsets of 13 compatible external experts plus the
embedding comparator, independent per-vibe pooling, fixed semantics, and expanded
shared-task training. Overlapping corpus versions replace each other in the
variant runs. The separate new-audio holdouts never enter this training pool.
The 159-song unlabeled pool has no known artist/title overlap with any of the
76 protected frozen local identities. Recording-held-out robustness is kept
distinct from artist-held-out accuracy.

`reserved freeze` requires every comparison stage and its output hashes to be
complete. It copies the unchanged checkpoint and all fixed candidate artifacts,
pins inference code and the two human snapshots, and rejects any declared fit on
the twenty reserved IDs. `reserved evaluate` verifies those pins, independently
replays complete consensus exports, computes every prediction before scoring,
and refuses to replace the first report. It reports all twenty additions, the
eighteen without known prior silver use, and the two prior-silver additions
separately. Per-vibe AP/AUC, selection/NLL and paired artist-group uncertainty
remain supplementary local diagnostics; the cohort is not used for selection.
The fixed candidates also include ordinary/consistency ridge and all six PMEmo
local-audio ablations. `local-export` refits those native heads after excluding
all 76 protected local identities, selects parameters on external native labels
alone, and applies the existing fixed VA rules without local-label calibration.
The exported fit dependencies retain earlier human-trained soft teachers;
reserved IDs are rejected if they appear in those declared dependencies.

`library inspect` finds 77 frozen recordings with three compatible samples from
one playback run. `library paired` fixes checkpoint, core/expanded consensus,
expanded rankings and fixed activity-supported semantics before opening the
reserved result. A process-local experiment adapter replaces those original
audio observations, runs production nonrecursive propagation and supplies the
result to actual Radio/Mix Tape consumers on copies of one frozen database.
Incomplete captures retain their identified original teachers. It is a consumer
replay, not a production-loader or update-invalidation acceptance test.
`library freeze-runtime` copies exact working-tree application source/assets and
benchmark helpers into a checksummed directory under `library-replay/`. Pass its
reported directory with the launcher's `--runtime-snapshot` option before
`library startup` or `library paired`. The bootstrap verifies every copied file
and rejects application imports from mutable checkout paths. It changes no
production files and includes uncommitted implementation bytes as recorded;
research results establish behavior of that snapshot, not subsequent changes.
The initial cold attempt is retained as incomplete because
`core/metadata_legacy.py` changed during execution. Its separate
`runtime-audit.json` identifies source drift without rewriting the first report.
Each paired copy retains identical prepared state (`--seconds 0`); bounded
preparation is measured separately. A checkpoint-selected eligible Radio seed
and the subsequent random stream are fixed across candidates and energy dials.
The seed's musical profile is recomputed for each candidate. Additional consumer
checks cover explicit include/avoid genre filters, pinned Radio genres and
exclusions on the same real library. Restriction checks read the candidate's
already published typed pools and verify that every consumer surface retains
the same generation; they do not rebuild the publication. Their existing
180-second overall bound now covers consumption alone. Empty restricted pools are reported as
coverage limits rather than filled with disallowed tracks.

The first paired replay reached the unchanged benchmark's 180-second publication
bound on the checkpoint control before consumer playlists were generated. Its
failure report, trace and database are retained separately. A second,
explicitly marked diagnostic uses `--publication-seconds 600` on that same SQL
snapshot and the frozen 839-file application runtime. Its experiments-only
driver calls the unchanged production publication and consumers, records plan
size before staging, and retains the original limits for other operations.
Completing this diagnostic does not pass the original 180-second attempt.
Publication for its checkpoint control took 157.844 seconds for 1,033 slots,
1,036 outputs and 67,931 candidate rows. The variation between attempts and the
process-local candidate adapter prevent treating these timings as a measured
production regression. The checkpoint control generated all Mix outputs and
three 24-track Radio sequences. Its twelve completed warm retunes each took
roughly 21–26 seconds; the run was intentionally interrupted during the thirteenth
to yield the machine slot to queued review checks. Partial timing and outputs
remain retained with the SIGINT reason. There is no complete warm aggregate,
restriction result or five-candidate comparison from this attempt.

New replays use `library paired-slice` to execute only the next candidate in the
same fixed five-candidate order. Pass `--resume` with the campaign directory for
the next invocation. Each invocation acquires and releases the machine lock unless
the user-authorized `--independent-execution` flag is present;
failed or interrupted attempts get separate directories. Resuming verifies the
SQL snapshot, model/input/source hashes, archived research source, runtime/package
versions, common seed and completed output receipts. Changed provenance requires
a new campaign. Completed candidates are never chosen again or silently replaced.
The new campaign also copies the immutable policy and prepared-output files
referenced by its SQL snapshot. Raw SQL backup alone retains absolute filenames;
the older attempts did not archive those external references. The experiment
copies only referenced files, checks each whole-file content address and native
record identity, then relocates filenames in its private `source.db`. Missing
original files fail the campaign instead of being replaced from another state.
The original source SQL is unchanged. A payload manifest, copied-file hashes and
the relocated SQL hash join the resume/end-of-run checks. Serving read guards
reject policy/output paths outside the active campaign, including sibling
attempts under the research workspace. These controls are
validated on the actual archived library for four completed candidates and the
retained failed attempt, with zero reads outside the boundary. They do not
retroactively strengthen the old attempts. This prepared-state replay does not
import or reconstruct native evidence-ledger history.
The driver closes each Radio reader before closing its private database.
Adapter diagnostics separately accumulate original profile-load, defensive-copy
and candidate-update wall time, including calls interrupted inside those phases.
Each measured phase records counter deltas; warm aggregates retain their sums
alongside wall/CPU totals. Payload read counts are recorded in the same phases.
Attempt reports retain these counters on failure as well as success. Across the
four completed candidates, measured profile load/copy/update phases total about
7–11 seconds of the 455–480 seconds spent on twenty warm Radio retunes. Those
adapter phases do not explain most of the observed wall time. Read guards and
other consumer work are not separately timed, so this does not isolate the
remaining cause or establish pure production latency.
Output comparisons retain unknown intensity separately from a known midpoint;
adjacent-pair summaries disclose how many pairs have actual profile evidence.
They also count the selected intensity input sources and vibe inputs. In this
frozen runtime, shared intensity selects the stronger usable Energize/Release fit
unless an explicit intensity override applies, then falls back to the profile
dimension when no usable fit exists. Changing a candidate's vibe observations
can therefore change both Mix Tape suitability and Radio intensity. These
derived scores do not independently validate perceived intensity or Release.
These describe the shared model's output, not independent human continuity truth.
These are experiment controls, not new production behavior. Example diagnostic
invocations under the user's independent-execution authorization:

```sh
./experiments/run.py --runtime-snapshot <runtime-directory> library paired-slice --independent-execution --publication-seconds 600 --database <frozen-source.db>
./experiments/run.py --runtime-snapshot <runtime-directory> library paired-slice --independent-execution --publication-seconds 600 --resume <campaign-directory>
./experiments/run.py --runtime-snapshot <runtime-directory> library verify-paired --resume <campaign-directory>
./experiments/run.py --runtime-snapshot <runtime-directory> library finish-constraints --independent-execution --resume <campaign-directory>
```

The retained campaign is
`library-replay/paired-slices-2026-10-29T150354.951331_0000`. Its relocated SQL,
84,051 payload references, 40,450 immutable files (250,910,161 bytes), frozen
839-file runtime, models, source archive and completed output receipts passed the
read-only audit. The audit explicitly reports all candidates attempted and all
candidates passed as different fields. Later research-driver edits are listed
separately and never substituted for the archived campaign code.

| Fixed candidate | Publication seconds | Twenty warm Radio retunes: median / p95 seconds | Restriction result |
| --- | ---: | ---: | --- |
| Checkpoint | 148.774 | 24.443 / 26.897 | 24 nonempty Mix conditions, three Radio conditions, zero violations |
| Core consensus | 158.121 | 23.557 / 25.499 | Same checks, zero violations |
| Expanded consensus | 138.615 | 23.916 / 26.748 | Same checks, zero violations |
| Expanded rankings | 224.950 | 22.873 / 26.312 | Same checks, zero violations |
| Expanded activity semantics | 188.815 | Incomplete: retune 13 reached 30.013 seconds | Separate continuation: 24 nonempty Mix conditions, three Radio conditions, zero violations |

All five produced six presets with four 12-track lanes and three 24-track Radio
episodes. Completed candidates retain publication generation 42 for restriction
checks, with no second publication. Their process-lifetime peak RSS ranges from
755 to 859 MB, including experiment setup; the failed semantic attempt reached
744 MB. Two publications exceed the original 180-second bound. These results do
not pass that original performance protocol.

The semantic restriction continuation is retained in
`constraint-followup-2026-10-29T162607.469072_0000/followup.json` within the
campaign. It reproduced all 27 ordered playlists exactly, then checked the
retained publication in 123.308 seconds with generation 42 unchanged. Its
publication took 184.733 seconds, also above the original bound. Runtime,
research code, frozen SQL and payload checks passed; 1,007 guarded output reads
stayed within the campaign. It did not retry warm retunes, alter the original
failed report or promote that candidate into the four completed methods.

Core/expanded consensus preserve checkpoint membership in all 24 Mix cards;
their Radio overlaps are 15/24 at low intensity and 24/24 at neutral/high. The
saved comparison summaries for those two candidates are identical. Expanded
rankings change more Mix membership (0–12 shared tracks per card) and overlap
12/24, 24/24 and 16/24 in Radio. A changed list is not a quality gain. Known-input
Radio intensity means are 0.155/0.497/0.848 for the checkpoint,
0.141/0.496/0.848 for consensus and 0.190/0.530/0.875 for rankings. Neutral Radio
has one unknown-intensity track in each case, retained separately from the
known-input mean. These remain model-derived descriptors, with no new human
continuity or intensity judgments.

`replay-stop <campaign-directory> --reason <reason>` sends SIGINT only to the
single matching owned replay process for this checkout, runtime and SQL source
or resumed campaign. It records the reason without discarding partial artifacts.

`library startup` clears derived content inputs, profiles and serving vibe
pointers on a private database, then records first profiles/vectors and bounded
remaining work. Collaborative history and mode definitions remain, so this cold
content check does not establish complete fresh-account startup.

The stable-runtime cold run produced its first 25 profiles and 19 content
vectors after 0.322 seconds of preparation. Its read-only backup took 14.469
seconds and private schema upgrade 6.118 seconds; those are separate from this
preparation timing. At the 120.303-second bound it had prepared 7,520 inputs,
3,741 library vectors and 1,442 discovery vectors. Work remained: 16,213 musical,
9,612 content and 1,717 vibe queue entries. The report therefore remains
`complete: false`, with unchanged runtime hashes. Early vectors use the initial
bounded basis and do not establish a completed full-corpus fit, complete consumer
output or end-to-end fresh-account latency.

`library fresh` starts a new empty database with no collaborative model, provider
history or prepared state. It imports the real compatible captured cohort using
production repositories, first with metadata alone, then one excerpt and then
three excerpts per recording. A missing-artist control must stay on heuristics.
Removing three private copied sample files must retire their current audio
evidence; a normal collection retry must restore the original scores without
deleting immutable measurement history. This is a bounded cohort preparation
check, not a complete account import or signed-in startup measurement.

The fresh-cohort run completed all five phases with unchanged production source
hashes. Metadata alone prepared profiles in 0.373 seconds; one excerpt remained
on heuristics. Three excerpts produced six direct audio vibe fits for all 77
eligible recordings in 1.210 seconds. Removing one recording's three private
files retired its current audio evidence; a normal retry restored exactly the
same scores. Immutable measurement history survived, relational checks passed,
and no developer annotations were imported. The missing-artist control remained
heuristic throughout. With title/artist data alone and no provider features,
genre/year or collaborative history, this cohort produced no content vectors;
the fit reports `waiting/empty-corpus`. Queue completion here establishes bounded
vibe preparation and recovery, not complete fresh-account recommendation output.

The additional `library fresh-consumers` run starts a new empty database and
imports the same 77 actual captured recordings through an isolated account and
playlist membership. It calls production Mix Tape and Radio consumers at worker
boundaries, first with title/artist data, then with selected genre/year values
copied read-only from the frozen SQL, one excerpt, and three excerpts. The copied
metadata is explicitly attributed to `frozen_catalog_replay`; it is not a new
provider lookup or evidence of a correct original recording match. There is no
collaborative history, development annotation, network enrichment or candidate
refitting. This also confirms that the earlier `upsert_track` preparation run
already created library membership through its default argument.

`fresh-consumers-2026-10-29T162220.046057_0000/report.json` records all four phases
drained in 38.056 seconds total. All six vibes first have two 12-track lanes and
all three Radio dials have 24 tracks at the first observed boundary, 3.934 seconds
after fresh-database setup began. These are upper bounds at observation
boundaries, include consumer probe time, and exclude preflight input verification
and scheduling idle time. The worker admission clock advances 155 seconds during
this 38-second run; it is not measured signed-in account startup latency.

| Arrival phase | Phase wall seconds, including probes | Content vectors | Vibe sources | Captured excerpts |
| --- | ---: | ---: | --- | ---: |
| Title/artist only | 16.887 | 0 | 462 heuristic entries | 0 |
| Copied genre/year | 5.155 | 77 | 462 heuristic entries | 0 |
| One excerpt | 3.932 | 77 | 462 heuristic entries | 77 |
| Three excerpts | 7.719 | 77 | 462 audio measurements | 231 |

All observed boundaries retain nonempty consumer outputs. The frozen SQL and
production runtime remain unchanged, relational integrity passes, and final
development annotations/collaborative vectors are both zero. Process-lifetime
peak RSS is 684 MB including setup. Availability before complete captures is a
fallback result; it does not establish measured vibe fit. The earlier
missing-file, retry and empty-artist controls remain complementary. Full account
import, scheduling, providers and listener suitability still need product review.

```sh
./experiments/run.py --runtime-snapshot <runtime-directory> library fresh-consumers --independent-execution --seconds 120 --database <frozen-source.db>
```

`comparisons` aligns historical out-of-fold IDs and folds before calculating
paired artist-group uncertainty against the same embedding baseline. It reports
predeclared procedures and individual experts without selecting a fixed subset
from the held-out results. These intervals condition on the fitted predictions;
they do not repeat model selection or correct for multiple comparisons.

The completed comparison aligns 55 recordings in 48 artist groups. The common
embedding reference has macro AP 0.39965, 34/55 acceptable top choices and NLL
1.03999. Each row below is the procedure selected inside its original training
folds, compared against that same reference. Positive AP gain and NLL reduction
favor the candidate. The AP intervals retain 1,989 of 2,000 group-bootstrap draws;
the remaining draws lack a positive example for one of the scored vibes.

| Fixed procedure | Macro AP | AP gain, conditional 95% interval | Acceptable top choice | NLL reduction, conditional 95% interval |
| --- | --- | --- | --- | --- |
| Core consensus | 0.43274 | 0.03310 [-0.02184, 0.11121] | 32/55 | -0.03009 [-0.13302, 0.05827] |
| Selected native/calibration learners | 0.44058 | 0.04093 [-0.01430, 0.10280] | 31/55 | 0.02376 [-0.05580, 0.10733] |
| Core suitability rankings | 0.53050 | 0.13085 [0.06340, 0.20102] | 29/55 | -0.01767 [-0.15591, 0.12448] |
| Aligned PMEmo and DEAM | 0.41651 | 0.01686 [-0.03799, 0.09607] | 31/55 | -0.05558 [-0.16488, 0.03694] |
| Perceived-emotion annotations | 0.40330 | 0.00365 [-0.03516, 0.06273] | 31/55 | -0.05389 [-0.15022, 0.02424] |
| Multiwindow Emotify | 0.43629 | 0.03665 [-0.01126, 0.10998] | 31/55 | -0.01759 [-0.13283, 0.07425] |
| Expanded consensus | 0.42804 | 0.02839 [-0.03213, 0.10832] | 33/55 | -0.07319 [-0.19313, 0.02473] |
| Expanded suitability rankings | 0.53301 | 0.13337 [0.06648, 0.20664] | 29/55 | -0.03328 [-0.16559, 0.10648] |

Suitability rankings improve historical AP over this embedding reference, but
their gains did not repeat against the unchanged checkpoint on the reserved
cohort. Every historical NLL-reduction interval includes zero. These repeatedly
examined developer labels remain exploratory; they supply neither new external
human evidence nor a reason to replace the checkpoint. The checkpoint's older
reported diagnostic uses different folds and is not paired with this table.
Read the retained results with `./experiments/run.py evidence --historical-summary`.

`capture` uses supplied music and unchanged production element-capture/frame
scripts in an isolated extension with the frozen research worker. It measures
actual PCM MediaRecorder decoding, downmixing/resampling and inference, then
compares native inference on the exact captured model input and ffmpeg decoding
of the same captured container. Research retains raw audio only under ignored
experiment artifacts. The controlled player does not establish signed-in site,
automatic scheduling, battery or service-worker suspension acceptance.
`preprocessing` reuses these exact captured containers and model PCM. It compares
default ffmpeg mono with an explicit arithmetic stereo mean and the browser's
44.1 kHz then 16 kHz contract. This separates downmix gain from resampling and
reports sensitivity of the frozen checkpoint; no input shifts, gain fitting or
padding improve the numerical comparison.
The lightweight `--decode-only` mode runs no model inference and explicitly
leaves the model comparison incomplete. Across the four retained captures,
default ffmpeg mono has a projected amplitude ratio of 1.383–1.416 relative to
the browser input. Explicit arithmetic averaging brings that ratio to
0.978–1.002 and reduces PCM root-mean-square error by factors of 2.9–119.
One recording still has a 0.02377 PCM error after arithmetic averaging;
matching channel weights alone does not establish decoder equivalence.
The full locked run completed all twelve native comparisons. Default decoding
changes a frozen checkpoint score by at most 0.03963; arithmetic averaging reduces
that maximum to 0.01617. None of the twelve comparisons changes its top choice.
This does not establish decoder equivalence: after arithmetic averaging, two
recordings still have maximum embedding differences of 0.99320 and 0.51034;
native tag differences reach 0.04643. The explicit two-stage rate contract gives
nearly the same results as arithmetic averaging alone. Four recordings cannot
establish population robustness or identify the remaining mismatch's cause.
Frozen features and models remain unchanged; a later frontend-contract decision
needs separate validation rather than retroactive replacement of this campaign.

The completed four-recording capture audit uses actual supplied Emotify audio
through `HTMLMediaElement.captureStream` and `MediaRecorder`, then unchanged
production decoding/framing and the frozen worker. Native inference on exactly
the browser's model PCM differs by at most 0.00001240 in embeddings and
0.000000517 in native tags. Each case takes 21.31–21.52 seconds including ten
seconds of capture, samples 12.01–12.16 seconds of browser CPU, and peaks at
505–529 MB process-tree PSS. These are controlled desktop measurements on four
recordings; they do not establish battery cost, signed-in playback or MV3
lifecycle acceptance. Independently decoded containers differ substantially in
embeddings, so the preprocessing decomposition remains necessary.

All 26 completion stages finished on 29 October at 11:24 UTC. Checksummed
collection finds no missing outputs or unexplained changes. Twenty-four model
artifacts passed a historical-input preflight and were frozen before the one
reserved evaluation. On the primary 18 additions without known earlier silver
use, checkpoint top-choice acceptance is 12/18 and acceptable-label NLL 0.96622.
Expanded consensus gives 13/18 and 0.96564; its paired NLL reduction interval
[-0.08216, 0.06830] includes zero. PCA/whitening gives 13/18 and 0.90112, also
inconclusive (NLL reduction interval [-0.04673, 0.15288]). Independent rankings
and the fixed activity-supported mapping have worse NLL in this small cohort.
These supplementary developer labels do not select a replacement checkpoint.
Full per-vibe metrics and every candidate remain in
`checkpoint-1/reserved-cohort/report.json`.
The supervised shared-native-quality head accepts 15/18 top choices, but its
NLL is 0.99083, with paired reduction interval [-0.33392, 0.20245]. Its higher
top-choice count supplies no replacement decision in this small cohort either.

The primary slice has four Relax, three Focus, six Cruise, twelve Energize,
six Uplift and **zero Release** positives (labels can overlap). Its macro AP
therefore averages five vibes, and it supplies no positive Release validation.
Local AP/AUC use unselected vibes as comparison negatives; these acceptable-choice
annotations do not establish that every unselected vibe is unsuitable. They
cannot supply explicit intrusion judgments for generated playlists.
Always choosing Energize also accepts 12/18 choices; aggregate top-choice
acceptance alone does not establish useful song discrimination. The unchanged
checkpoint's Focus AP is 0.16389 against prevalence 0.16667, with AUC 0.33333.
`evidence --reserved-controls` adds a separate, explicitly post-hoc descriptive
report. Frequencies from the original 55 compatible human examples give NLL
1.07872 and macro AP 0.34444 on these 18 additions, versus checkpoint 0.96622
and 0.52874. The historical prior chooses Cruise, its most frequent training
label; a uniform control gives NLL 1.29115. These controls leave the frozen plan,
model artifacts, predictions and first report unchanged and do not select a model.

The PMEmo protected refit also selects zero local-neighborhood, external soft
target and stored-teacher weight. Ordinary ridge and consistency ridge produce
identical reserved predictions. This is evidence against claiming an unlabeled
or stored-prediction gain from this small compatible pool; thousands of derived
scores still do not supply independent measurements.

The current model decision is to retain the checkpoint. Native external tasks
and ManyMusic/Memo transfer establish useful emotion prediction on supplied
audio; they do not establish a general six-vibe improvement. No candidate has
demonstrated a reliable reserved-cohort gain, Focus remains weak, and the primary
reserved slice cannot validate positive Release predictions. A further model
checkpoint should first specify and validate the audio preprocessing contract,
then test a focused hypothesis using independent native activity/emotion
judgments. Keep the new-audio holdouts out of fitting and parameter selection.
Broader expert searches or additional stored prediction targets are not justified
by the present results. Remaining consumer and preparation checks measure
operational behavior and cannot substitute for human listening judgments.

`checkpoint-1/inputs/manifest.json` pins human labels, copied immutable capture
files, current predictions and their measurement contributors, and the external
feature manifests acquired at that point. Later corpora need a new overlap
manifest before a pooled comparison; this initial manifest must not be silently
rewritten. The human export and wider lineage export have separate read
transactions. Frozen counts are point-in-time observations.

The first snapshot has 76 human-labeled tracks with compatible embeddings,
including 20 additions to the earlier 56-row gold snapshot. Reserve additions
from model selection. Two appeared in the historical silver pool, so the whole
addition cohort cannot be described as an untouched prospective test. Native
external supervision and cross-corpus transfer remain the primary evidence.

The snapshot has 7,610 recordings with current vibe scores, 88 recordings with
captured embeddings, and 12 captured embedding recordings without human labels.
One of those 12 shares an exact normalized artist with a labeled track.
`checkpoint-1/library-audit.json` records source counts, model versions, missing
lineage, repeated contributor sets, and possible evaluation contamination.
Thousands of propagated scores are not thousands of independent audio examples.
Recompute propagation within training folds and count original contributors.
The compatible local audio pool retains 12 recordings, eight with complete soft
teacher lineage. Four of those eight use the product Emotify model, which was
fitted on all 400 Emotify targets and 55 local labels. They cannot supply an
independent Emotify or historical-local holdout. Provider and heuristic teachers
need separate provenance and ablations; completeness alone does not establish
independence.

Completed first comparisons:

| Native task | Held-out observation | Interpretation |
| --- | --- | --- |
| Emotify, 400 audio excerpts, 138 artist groups recovered from audio tags | Embedding MAE 0.11303; training mean 0.14503 | Supersedes the earlier recording-only validation for this comparison. |
| Emotify listener weighting, same artist folds | Nested selected exponent MAE 0.11293 | Very small change; no established improvement. |
| Raw CAL500 activity votes, 496 audio recordings | Embedding macro AP 0.4171; constant median 0.2746 | Ranking is useful despite sparse-label MAE favoring the median baseline. |
| Raw CAL500 Reading / Studying / At work | Embedding AP 0.4595 / 0.5108 / 0.2005; prevalence 0.2762 / 0.3306 / 0.2218 | Separate activities disagree; a general Focus improvement is unproven. |
| Emotify+, 265 strict audio joins with artist grouping | Embedding MAE 0.12523; training mean 0.12445; pretrained mood features 0.12312 | Additional listeners did not improve this embedding-only native prediction task. Retain the negative result. |
| PMEmo, 767 supplied choruses, 267 connected artist groups | Embedding MAE 0.08597; training mean 0.14853 | All known `+` artist credits are grouped transitively. This replaces first-artist-only scores for the current checkpoint. |
| AMG1608 matched to existing audio, 404 excerpts, 310 groups | Embedding MAE 0.09114; training mean 0.12804 | Reuses audio rather than recovering recordings from track lists. Song-level labels and excerpt mismatch remain weak supervision. |
| NTWICM matched to existing audio, 55 recordings, 50 groups | Embedding MAE 0.11535; training mean 0.13311 | Small matched subset, not a reproduction of the original full-song benchmark. |
| MERP static pilot, 50 songs, 43 artist groups | Embedding MAE 0.05572; training mean 0.10371 | Excludes four DEAM anchors. Timeline-average labels and a center excerpt are not temporally aligned supervision. |

These native activity labels are listener judgments, with disagreement and
cultural/contextual limits. They provide evidence independent of developer vibe
labels; they are not direct six-vibe ground truth.

The first local-audio ablation uses 12 captured recordings, eight with complete
stored teacher lineage. On the corrected PMEmo folds, neighborhood and fresh
external soft-target selection choose zero local weight in every fold, exactly
matching the 0.08597 linear control. Stored-teacher shared training has MAE
0.09066 versus 0.09009 without local inputs; the conditional paired group
bootstrap interval for improvement is [-0.00150, 0.00094]. Soundtracks also shows
no established local-data gain. These experiments do not support adopting stored
prediction targets. Full method/fold exports are in `checkpoint-1/`.

Pure propagation replay on the frozen real library also needs conservative
interpretation. On the historical 55 human examples, macro AP is 0.3628 when
hiding only the recording and 0.3186 when excluding all recordings sharing known
artist credits; the neutral baseline is 0.2697. Artist-held-out macro AUC is only
0.5135. Agreement with 2,395 withheld model measurements is consistency evidence,
not human accuracy. Original contributors are deduplicated, descendants add no
votes, and a seed retraction matches a complete rebuild. Coverage, abstentions,
missing metadata and seed-count slices are in `propagation.report.json`.

The isolated real-library workflow audit completed with the unchanged product
model `5351572e6eb1...`, stable runtime source hashes and zero network requests.
It generated 24 Mix Tape cards (four lanes for each of six vibes), each containing
12 tracks, plus three 24-track Radio episodes at intensity 0, 0.5 and 1. The
listening packet has 240 unjudged tracks and 20 intensity pairs with artist,
album and genre holdouts. Warm Mix Tape assembly medians were 43–46 ms; warm
Radio retuning and four direction plans had median 0.864 s and p95 1.210 s.
These timings exclude HTTP and remote playlist writes. Preparation remained
incomplete after its 120-second budget; this audit retained prepared state and
does not establish fresh-user startup or listening quality. The reported 2.31 GB
peak is the lifetime peak of the combined research process, not isolated workflow
memory. See `checkpoint-1/listening-workflow-index.json` and its hashed artifacts.

The runner also supports `native-ready`, `secondary`, `learners-ready`,
`temporal-pmemo`, `temporal-deam`, `cross-corpus`, `workflow`, `overlaps`,
`consensus-core`, `consensus-selected`, `ranking-core`, `semantics-core`,
`projection`, `multitask`, `merge` and `merge-expansion`. The selected consensus
chooses native learner families on native training labels, then calibration
families and all nonempty expert subsets inside the local training folds. The
reserved additions supply identity exclusions only. Later overlap manifests are
content-addressed and preserve the initial snapshot. `--keep-going` records a
failed stage and continues independent stages; outputs are never silently
accepted as complete if research code changes while a stage is running.

Earlier validation: `./experiments/run.py check` passed all 40 focused checks, including
released decimal intervals, strict real-audio coverage, original-cache hashes,
runtime identity, protected local identities, and duplicate audio crossing an
unlabeled training/holdout partition. It uses `uv run pytest
--test-lock-wait=60 -p tests.conftest` and the seven focused research test files.
The normal shared machine lock was respected; lock-busy attempts collected no
tests. Launcher dispatch, help, status, logging and argument boundaries also
passed lightweight checks without starting another heavy session.

An interrupted DEAM extraction exposed a dependency problem: downstream stages
could consume a partial feature manifest. The runner now requires every indexed
source identity to have a result or a recorded extraction error before reusing
or evaluating it. Seventeen reports from the 330-row partial manifest are
preserved under `checkpoint-1/partial-source-330/` as bounded pilot artifacts;
they do not establish the complete-corpus comparisons. Failed stages remain in
the saved history while the current campaign reruns them.

The running checkpoint also preserves its original research runtime under
`local/runtime-checkpoint-530/`. `runtime_snapshot.py` recovers only Git objects
whose hashes match the earlier extractor provenance, preserving the existing
feature signature after concurrent production runtime moves. Current production
files are not restored or changed by this snapshot.

The independent native/Node/browser probe is reproducible with:

```sh
./experiments/run.py reference
```

It uses hash-pinned original `.pb` graphs and an isolated
`essentia-tensorflow==2.1b6.dev1438` install under `local/reference-python`.
Four fixed Emotify IDs (1, 101, 201, 301) cover supplied recordings in its four
genres. Native C++ and Node receive identical FFmpeg-decoded 16 kHz PCM and
matched frame/patch policy. Maximum absolute differences were 0.00000386 for
embeddings, 0.000000229 for tags and 0.000000557 for mood heads. Chromium
148.0.7778.96's unchanged research web worker differed from Node by at most
0.00000891 for embeddings and 0.000000398 for tags. Mood outputs retained in the
report differ by at most 0.00000150. These are bounded numerical observations,
not evidence about emotion accuracy.

The browser's four ten-second inputs took 9.18–13.85 seconds each, including
0.465 seconds initial model setup on the first request. Sampled process-tree
CPU was 9.4–14.26 CPU seconds per input. Peak PSS was 340,272,128 bytes; summed
RSS reached 534,257,664 bytes and double counts shared pages. Sampling every
100 ms can miss shorter peaks. Native matched-policy frontend/graphs took
0.262–0.274 seconds, excluding startup and extra wrapper comparisons. These
results leave browser inference cost as an explicit #406/#424 gate.

The published native wrapper has a different framing/overlap policy: its mean
embedding difference from the matched policy was 0.130–0.197 across these cases.
Preserve those outputs rather than claiming default-wrapper equivalence.
This probe does not exercise tab capture, browser downmixing/resampling, MV3
lifecycle or battery use. It makes no recommendation adoption. Full results,
input PCM hashes, graph hashes and resource scopes are in
`checkpoint-1/reference-pipeline/report.json`.

Two additional audio-backed releases:

- [Emotify+](https://data.mendeley.com/datasets/wbk8zr9bd2/1) adds perceived-emotion
  annotations to existing Emotify audio. The pinned CSV has 3,031 rows, 399 file
  identities and 174 listener IDs. Conflicting titles or genres exclude complete
  tracks from the primary join, leaving 265 verified tracks. Listener-level
  deduplication prevents repeated responses adding votes. Selection frequency and
  conditional intensity remain separate. This source shares Emotify's audio and
  must share its artist/recording exclusions; it is not independent test audio.
- [MERP](https://huggingface.co/datasets/amaai-lab/MERP), pinned revision
  `04d90b60d92f079f2e25759e48f233094f7af84a`, supplies 54 WAV files and filtered
  annotations from 277 raters. The static pilot retains 50 songs in 43 artist
  groups and excludes all four DEAM anchors. It averages repeated trials within
  listener/song before averaging listeners. The author's processing code
  resamples to 0.5-second steps and removes the start, despite the README's
  original 10 Hz description. Exact temporal alignment remains unverified;
  average timeline judgments are explicitly weak static targets for a center
  excerpt. Rater demographic profiles are not training inputs.

The feature bundle contains a DEAM-trained valence/arousal head. New DEAM native
comparisons exclude those two outputs from every feature family and from direct
pretrained predictions. The embedding and attribute controls remain available.
Most DEAM artist identities are missing, so its grouped results cannot establish
unseen-artist generalization. Unknown upstream audio overlap remains a limitation.

Acquisition and audit commands, separate from heavy fitting:

```sh
.venv/bin/python -B -m experiments.audio_lab_tools.external_library_audit
.venv/bin/python -B -m experiments.audio_lab_tools.external_merp acquire
.venv/bin/python -B -m experiments.audio_lab_tools.external_merp inspect
```

MERP acquisition requires its pinned Hugging Face tree in
`local/datasets/merp/release-tree.json`; use `recursive=true&limit=1000` without
expanded security metadata to retrieve all entries. Each supplied file is
checked against the tree's Git LFS SHA256 or Git blob SHA1 and byte count. The
optional Parquet reader is installed under ignored `local/python-deps/`, outside
product dependencies. No dataset audio or experimental model is redistributed.

## Historical #507 procedure

Run the complete local-audio sequence from the repository root:

```sh
.venv/bin/python -m experiments.audio_lab_tools.external_experiment suite --native
```

This command requires no network access or further downloads. Its native Node
dependency is already installed under ignored `local/node/`, separate from the
product dependencies. One low-priority extraction worker processes one recording
at a time with one-thread native kernels and FFmpeg. Experiments do not acquire
or bypass the pytest lock. A separate experiment lock prevents duplicate suites.

The sequence is Soundtracks Set 2, CAL500, Emotify, PMEmo, then the alternate
Soundtracks Set 1+2 experiment. Features are cached by source-audio hash,
extractor/model provenance, backend, decoder version, and excerpt position.
Rerunning resumes from cached successes. Individual corpus failures are recorded
and do not stop subsequent corpora.

## Evidence and progress

- `suite-status.json`: current corpus, training/transfer outcomes, and protected
  active-model hashes.
- `<dataset>.status.json`: extraction count, errors, elapsed time, and timestamp.
- `<dataset>.index.json`: exact audio/annotation joins and grouping identities.
- `<dataset>.features.json`: successful extractions, errors, and provenance.
- `<dataset>.report.md` and `.report.json`: native-label comparisons.
- `<dataset>.oof.csv` and `.splits.json`: auditable held-out predictions and folds.
- `cal500.ranking.md` and `.json`: activity average precision, AUC, and human-vote
  weight lift among the ten highest-ranked tracks in each held-out fold.
- `annotation-audit.json`: raw response counts, descriptive split-half stability,
  and reconstruction of CAL500 activity aggregation from individual annotations.
- `emotify.genre-check.md` and `.json`: genre-only baseline and prediction
  correlations within each genre, computed from the same held-out folds.
- `models/`: experimental regression coefficients, in a research-only format.
- `transfer-<datasets>.report.json`: diagnostic transfer to the existing gold
  snapshot; no prediction-only examples are promoted to human labels.
- `transfer-status.json`: progress of the transfer recovery worker. The initial
  suite imported the transfer module before a missing-embedding fix; its original
  error remains in `suite-status.json` as history, while corrected comparisons
  are produced by `.venv/bin/python -m experiments.audio_lab_tools.external_transfer --watch`.

When output is redirected to `suite.log`, it remains a readable progress log.
The status files update after each ten extracted examples and each training
stage. A complete suite takes roughly two hours on the measured one-worker
throughput; the first useful corpus comparison is produced much earlier.

## What is compared

Decode the center ten seconds of each supplied recording with FFmpeg. Feed the
same excerpt into the unmodified Radio research extractor: 27 audio attributes,
the 200-value frozen MusiCNN embedding, and six pretrained mood outputs. The
research Node harness uses native TensorFlow kernels. Its initial parity check
against TFJS CPU found identical attributes, a maximum embedding difference of
0.00000644, and a maximum mood-score difference of 0.00000073. FFmpeg resampling
differs from the browser's audio decoder; browser bitwise equivalence is not
claimed.

Fit separate native-label regression tasks for each dataset. Compare attributes,
embeddings, pretrained moods, attributes plus embeddings, and all three. Keep
each dataset's meanings and scales separate. Normalize native labels to 0–1;
use published soft annotation weights as targets, not automatically hard classes.

Nested validation uses five outer grouped folds and three inner grouped folds.
Imputation, standardization, and regularization selection use training folds
only. Baselines predict the training mean and training median. Reports include
MAE, squared error, per-target correlations, and paired group-bootstrap intervals.
CAL500's mood/activity targets choose regularization; its other tags are retained
for inspection. Sparse-label results must also be assessed against the median
baseline and correlations, not solely the training mean.

Grouping keeps Soundtracks films and linked excerpts together where identities
are supplied, CAL500 artists together, Emotify recordings together, and PMEmo
primary artists together. Identical encoded audio also shares a group. Set 1
has incomplete film identities, Emotify lacks artist identities, and PMEmo guest
artists can overlap across groups; these limitations are recorded in reports.

After each independent corpus finishes, use its embedding-based heads as frozen
quality features for the six existing vibes. External examples matching a gold
artist or title are excluded before fitting these transfer heads. Check exact
embedding/model-weight provenance against the saved gold snapshot. Compare
embedding-only learning with external-quality features and their combination,
using nested artist-group validation on the 55 developer-labeled songs with
embeddings. One of the 56 gold songs has no embedding in the saved snapshot;
it is explicitly excluded from this comparison and its original label remains
unchanged. The 63
prediction-labeled songs remain a separate silver set and are excluded from this
diagnostic. This small, previously examined gold snapshot cannot establish
product-wide quality.

## First completed comparison

Soundtracks Set 2: all 110 excerpts extracted, 37 validation groups, no extraction
errors. Mean held-out absolute error across eight normalized emotion ratings:

| Features | MAE |
| --- | ---: |
| Training mean | 0.1890 |
| Training median | 0.1807 |
| Attributes | 0.1616 |
| Embeddings | 0.1121 |
| Pretrained mood outputs, fitted to this corpus | 0.1300 |
| Attributes and embeddings | 0.1133 |
| Attributes, embeddings, and mood outputs | 0.1127 |

These results support native-emotion learning from the supplied annotations.
The separate 55-song vibe diagnostic found no improvement from adding the
Soundtracks heads to embeddings: both achieved 61.8% top-choice acceptability,
with slightly worse acceptable-label log loss for the combined features. CAL500
adds activity judgments and must be assessed separately. Full evidence is saved
in `local/external-20261028/soundtracks-set2.report.json` and
`transfer-soundtracks-set2.report.json`.

CAL500 has 496 usable recordings. Four supplied recordings are shorter than ten
seconds, and two annotation rows lack supplied audio; exact exclusions are saved
in the join audit and feature manifest. Activity tags are sparse: the median
baseline beats the fitted models on aggregate absolute error. Ranking provides a
different useful check. For embeddings, party average precision is 0.390 against
0.125 prevalence; going to sleep is 0.285 against 0.111; exercising is 0.142
against 0.044. Attributes reach 0.347 for going to sleep and 0.220 for exercising.
Work and studying remain weak. These exploratory activity results do not establish
six-vibe improvement. Adding Soundtracks and CAL500 heads to embeddings gets one
more gold song right (63.6% versus 61.8%), while acceptable-label log loss worsens
from 1.040 to 1.069.

The raw-annotation audit reconstructs all 7,530 supplied CAL500 activity targets:
the published weight is the Yes fraction when at least two listeners select the
activity, otherwise zero; binary activity targets indicate nonzero weight. This
differs from the README's stated 80 percent consensus rule. A separate
`cal500-raw-activity` experiment retains the unfiltered Yes fractions. Omitted
checkboxes are interpreted as unselected. Its target definition differs from the
published benchmark, which remains intact. `annotation-audit.json` also records
descriptive listener split-half stability for CAL500 and Emotify.

Emotify completed all 400 excerpts without extraction errors. Embedding MAE is
0.1103 versus 0.1444 for the training mean and 0.1372 for a training-genre-mean
baseline. Within-genre mean target correlations range from 0.381 to 0.573;
calmness, power, and joyful activation correlations across the corpus are 0.617,
0.588, and 0.661. Artist identities are unavailable, so these are recording-held-out
results. Adding its heads to Soundtracks and CAL500 still gives 63.6% versus 61.8%
top-choice acceptability on the small vibe diagnostic, with worse log loss
(1.064 versus 1.040). Native emotion learning is useful evidence; convincing
six-vibe improvement remains unproven.

PMEmo completed all 767 supplied choruses without extraction errors, across
398 primary-artist groups. Embedding MAE is 0.0844 versus 0.1443 for the training
mean; attributes plus embeddings reach 0.0840. These remain native
arousal/valence results, separate from the small six-vibe diagnostic.

```sh
.venv/bin/python -m experiments.audio_lab_tools.external_annotations
.venv/bin/python -m experiments.audio_lab_tools.external_ranking
.venv/bin/python -m experiments.audio_lab_tools.external_strata --wait
```

## Unrated-audio consistency experiment

The consistency run waits for the native suite to finish, then selects 160 PMEmo
entries by a seeded audio-content hash. A subsequent audit found 159 unique
recordings: one repeated entry was confined to the training partition. The
original 128 training entries therefore contain 127 unique recordings; all 32
held-out recordings are distinct and disjoint from training. Preserve the original
run, and repeat it with the duplicate removed. Selection does not use emotion labels,
vibe labels, or prediction scores. Exclude known artist, title, and identical-audio
overlaps with CAL500 and all gold songs. Preserve this selection and all exclusions
in `local/external-20261028/unlabeled-consistency/pool.json`.

Create one real audio augmentation of the same center excerpt: attenuation
between 3 and 9 dB plus noise at 35 dB signal-to-noise ratio. Collect its frozen
embedding alongside the existing clean embedding. No distant song sections are
forced to agree, and no prediction becomes a training label.

Use 128 pairs for learning and reserve 32 separate recordings for robustness
measurement. Reserved pairs never enter fitting or parameter selection. Report
prediction changes alongside prediction variability to expose a constant-output
shortcut; these measurements supplement the labeled held-out quality comparisons.

Compare ordinary ridge training with a consistency-regularized version. The new
term penalizes squared differences in predictions across these unlabeled audio
pairs; existing external human annotations remain the supervised target. Tune
its strength, including zero, inside the existing nested grouped folds. Compare
Soundtracks emotion prediction, CAL500 mood/activity prediction, and the preserved
55-song vibe diagnostic. Export research coefficients and held-out predictions
without activating a candidate.

```sh
.venv/bin/python -m experiments.audio_lab_tools.external_consistency --wait-for-suite --pairs 128
.venv/bin/python -m experiments.audio_lab_tools.external_results --watch
```

The combined results are written to `local/external-20261028/RESULTS.md`.
The augmentation worker passed a real-audio pilot. With consistency strength
zero, its predictions matched ordinary ridge to a maximum absolute difference
of 0.000000000000002. Quality improvement from unrated audio remains unproven
until the held-out comparisons complete. The original completed comparison
improved Soundtracks emotion MAE from 0.1121 to 0.1082, left CAL500 effectively
unchanged, and worsened the gold vibe log loss. That is a limited native-task
gain, not established six-vibe improvement. The duplicate-free repeat is queued
with separate artifacts.

## Dataset-expert consensus

Each completed native dataset supplies a separate expert. Calibrate each expert's
native qualities to the six preserved vibes inside artist-separated training
folds. Compare equal probability pooling and learned convex pooling, both with
and without the embedding-only expert. Learn mixture weights from predictions
that exclude the corresponding song and artist; select each expert's ridge
strength inside that meta-training split. The entropy prior toward equal weights
is fixed before evaluation. No prediction becomes a human annotation.

The consensus search also compares every subset of the available experts, with
equal and learned pooling for every multi-expert subset. Four dataset experts
plus embeddings give 31 nonempty subsets (26 consensus subsets); VGMIDI expands
this to 63 (57 consensus subsets). The alternate overlapping Soundtracks-all
corpus is not counted as another independent expert.

`selected_combination` chooses a multi-expert subset and pooling policy within
each outer training fold. `selected_candidate` also permits a single expert.
Both use acceptable-label log loss on cross-fitted inner expert predictions;
those inner labels fit the pooling weights as well as rank the candidates.
Only the outer fold estimates the complete selection procedure. Fixed-subset
outer scores are also retained, but choosing their apparent winner afterward
does not independently validate it. Save the full search, fold selections, and
weights in the report and exported research model.

The first three-dataset comparison gives 63.6% acceptable top choices and 0.979
log loss for learned external consensus, against 61.8% and 1.040 for embeddings.
Emotify alone gives 69.1% and 0.928. This is the same previously examined 55-song
diagnostic, so selecting the best result afterward cannot establish a final model.
Agreement checks are inconclusive; these experts share embeddings and can share
errors. Keep per-fold weights, individual expert scores, error correlations, and
disagreement diagnostics in `consensus-*.report.json`, with research coefficients
under `models/`.

The four-dataset search (Soundtracks Set 2, CAL500, Emotify, PMEmo, plus the
embedding expert) completed all 31 nonempty subsets and 57 fixed candidates:
five individual experts and two pooling policies for each of 26 multi-expert
subsets. No fixed combination beats Emotify alone on either acceptable top
choices (69.1%) or acceptable-label log loss (0.928). The highest fixed-combination
top-choice score is 67.3%; its best log loss is 0.952. Choosing the combination
inside each training fold gives 63.6% and 1.000; allowing a single expert to win
the same search gives 63.6% and 1.007. These are exploratory results on the
previously examined 55 songs, not a final model selection.

```sh
.venv/bin/python -m experiments.audio_lab_tools.external_consensus --watch
```

## Additional supplied-audio queue

VGMIDI's author repository contains 99 rendered WAV files, now downloaded into
ignored `local/datasets/vgmidi/source.git`. Seventy-six recordings passed the
join checks. The follow-on runner verifies
original annotation titles, franchise identities, and durations before pairing
the WAVs with complete listener annotations. It uses equal-listener whole-piece
means from the original time-varying annotations, a separate weak-label task from
the published phrase classes. It does not render new MIDI or substitute game
soundtrack recordings.

The VGMIDI extraction waits until the initial suite and unlabeled consistency
finish, preserving one extraction worker. It then trains native-label models,
updates transfer comparisons and consensus, and saves its join audit and errors.
Composer identities are unavailable; validation groups related game franchises.
This supplied-audio subset is not the complete 200-piece collection.

```sh
.venv/bin/python -m experiments.audio_lab_tools.external_vgmidi
```

`additional-datasets-status.json` records acquisition, verified rows, queue state,
and completion or failure. The combined results watcher follows this extra queue.

## Expanded campaign and follow-up trials

The current sequential campaign adds 216 verified Jamendo human-annotated tracks,
903 Coimbra MIREX-like clips, 900 4Q clips, and 1,802 DEAM recordings. The original
and expanded Soundtracks datasets replace one another in mixtures. After native
comparisons it runs all expert subsets, with alternate native learners and vibe
calibrators. `campaign-status.json` and `campaign-expanded.log` record progress.

```sh
.venv/bin/python -m experiments.audio_lab_tools.external_campaign
.venv/bin/python -m experiments.audio_lab_tools.external_followups
.venv/bin/python -m experiments.audio_lab_tools.external_results --watch
```

The follow-up runner waits for that extraction campaign, then reuses its cached
features for original AMG1608 and NTWICM song-identity intersections, native
classification, time-aligned DEAM/PMEmo supervision, unlabeled PCA/whitening,
duplicate-free consistency, and a small shared nonlinear representation. Every
trial retains its own report, predictions, provenance, and research model. Failed
stages are recorded without blocking unrelated remaining trials.

The product ranks songs within a selected vibe. A separate ranking experiment
therefore fits six independent suitability scores and tests every expert subset,
including learned weights specific to each vibe. Selection uses nested
artist-separated class-balanced binary log loss; reports retain per-vibe average
precision and AUC. Unselected alternatives in the old gold snapshot are not
explicit human rejections, and the scores are not population probabilities.

The first ranking pilot uses Emotify and PMEmo plus embeddings. Its held-out macro
average precision is 0.586 for Emotify, 0.446 for embeddings, and 0.553 for the
inner-selected combination. This is promising diagnostic evidence for an Emotify
head; it is still the same small, previously examined developer snapshot. The
larger ranking search and the shared-representation comparison remain queued.
Reports are `vibe-ranking-*.report.json`; exports are `models/vibe-ranking-*.json`.
