# Complete-output evidence ablations (#587)

This is a controlled comparison of metadata, captured signal, and their
combination on one isolated prepared-account snapshot. It does not refit models
or certify deployment, startup, rendered UI, real extension scheduling or
subjective listening benefit. Independent acceptance belongs to review.

## Frozen inputs and scope

The read-only account export started on 30 October 2026 at 20:05:44 UTC. It
contains 14,332 recordings, 10,254 retained metadata profiles, 2,607 original
provider-measured recordings, 124 complete captured recordings and 46 recordings
with only missing/partial captures. Of the 124 complete captures, 83 have
compatible embeddings and use all six specialists; 41 legacy captures lack
usable embeddings and retain the production audio heuristic. All 121,677 referenced files
and all 715 captured sample files were copied; none were missing. Three retained
profiles contain conflicting dimensions. The account still has 311 content,
14,036 musical and 2,776 vibe preparation entries; this is not a fully converged
or fresh-account acceptance claim.

The frozen SQL SHA256 is
`c5a267a7c9bb87e42956a8596d51a6dc93714829d2e7e1324ea785fcdc83bdce`.
The complete 1,040-file runtime manifest SHA256 is
`160c16c69627533785c94624a6efcbcc5acbdd4b6ece84a3d8af2979014ad7e2`.
The plan pins all six specialists and the shared artifact that still supplies
intensity. This is the frozen working-tree composition, not a released build.

| Artifact | SHA256 prefix |
| --- | --- |
| Cruise | `3927a3294ad7a167` |
| Release | `3155a3a5dd44776f` |
| Relax | `4fc43b28a71304ab` |
| Uplift | `b29217c7f706c9a5` |
| Energize | `ee27a94000a37b02` |
| Focus | `695f07272d12d218` |
| Shared classifier/intensity | `5351572e6eb1bc17` |

Full hashes and paths are in `round-4/plan.json`. All specialist modes are
explicitly active. The reporter independently replays every complete capture,
requires all six exact specialist identities on compatible inputs, checks exact
heuristic fallback on incompatible inputs, and compares every direct suitability
and intensity score to the served profiles. All 124 replay exactly, with maximum
score error zero. The initial audit rejected a legacy capture because it wrongly
assumed every complete capture had embeddings; the repaired audit classifies
input compatibility explicitly and still rejects any missing specialist on a
compatible capture. No arm was rerun or its output changed for this correction.

The metadata arm discards previous audio fits and previous propagated scores.
It uses retained metadata dimensions and current original provider measurements.
The signal arm uses the current complete-capture inference path, including its
legacy audio heuristic fallback, with neither
metadata musical dimensions nor artist/genre propagation. Missing audio stays
unknown. Combined uses metadata fallback, gives compatible direct audio
precedence, and applies the production one-recording, nonrecursive propagation
policy. Catalog identity, taste vectors, eligibility and explicit genre controls
remain available to every arm. An adapter supplies the profiles to unchanged
production candidate publication and consumer assembly.

The metadata baseline uses currently referenced provider measurements and
retained prepared cohort keys; it does not resurrect superseded provider fits
or finish the account's pending metadata/cohort preparation. This is a declared
ablation of the retained state, not a replay of every upstream preparation job.

The first-excerpt condition simulates a first incomplete capture before any
usable audio measurement. It does not erase a previous complete measurement in
a continuing production session. Actual continuing-input preparation evidence
is reused separately below.

## Reference limits

The fresh local export contains 85 usable labeled recordings, one new identity
relative to the previous 84-row freeze. It retains separate exclusions for four
old-vocabulary answers, five superseded answers, five empty/cleared answers,
two missing/invalid embeddings and three answers without available samples.
These are excluded answer counts, not necessarily disjoint new song counts.

Selected vibes are positive references; unselected vibes remain unknown. Exact
recording and inference-window joins are reported separately. Positive-only
score error cannot establish false-positive rates, whole-list precision,
unsuitable inclusions or superiority of a recommendation list. An always-high
score would do well on this restricted diagnostic. Previous inspection and
source-training exposure prevent interpreting this as a new holdout.

All 85 labels join to exact account recordings; 73 also match the inference
sample IDs. Eighty-four were already inspected. The exact-window subset contains
18 Focus, 14 Relax, 32 Energize, 18 Uplift, 34 Cruise and six Release positives;
selections overlap. Intervals resample connected recording/artist/album groups
with 2,000 draws and seed 136, conditional on these frozen fitted models.

External native and fixed-mapped sources remain separate from local labels.
Only verified recording/version/window identities could transfer their labels
to these outputs; matching a title or artist is insufficient. Model predictions
and propagated fits never become reference truth.

The external inventory covers eleven source cohorts and 5,725 reference rows.
None supplies a verified local recording/version/window crosswalk. Both native
and fixed-mapped complete-output metrics therefore remain unavailable.

## Complete-capture Mix outputs

Each arm produced four 12-song cards for each of the six vibes, with 48 distinct
recordings per vibe. Catalog eligibility, clock, random seed and explicit
restriction cases are fixed across arms.

| Vibe | Signal direct-audio slots / 48 | Combined direct-audio slots / 48 | Combined specialist slots / 48 | Combined recordings shared with metadata / 48 |
| --- | ---: | ---: | ---: | ---: |
| Relax | 12 | 2 | 0 | 45 |
| Focus | 7 | 4 | 0 | 43 |
| Cruise | 36 | 34 | 25 | 16 |
| Energize | 33 | 4 | 1 | 44 |
| Uplift | 24 | 9 | 1 | 38 |
| Release | 26 | 25 | 0 | 10 |

The other signal-only slots have zero-support heuristic fits. Combined preserves
provider evidence and conservative inheritance: its Release cards contain 25
direct audio, eight provider, thirteen inherited and two heuristic slots.
Cruise and Release show the largest membership changes from metadata-only.
These are selection and evidence-coverage results, not judgments that the new
lists sound better. Ordered identities and per-card coverage remain in the
retained output files and comparison report.

Direct audio does not necessarily mean a learned specialist. All 25 direct-audio
Release slots in combined use legacy heuristic captures; its specialist scores
do not reach these cards. Combined Relax and Focus likewise have no directly
specialist-scored slots. The score scales of specialists, provider evidence,
legacy heuristics and inherited estimates require explicit treatment when
retiring fallback under #642. These observations do not establish unsuitable
inclusions or justify reopening an accepted model on their own.

Exact-window selected-vibe positives in the 48 output slots are:

| Vibe | Metadata | Signal | Combined |
| --- | ---: | ---: | ---: |
| Relax | 0 | 2 | 0 |
| Focus | 0 | 0 | 0 |
| Cruise | 0 | 10 | 10 |
| Energize | 0 | 12 | 1 |
| Uplift | 1 | 1 | 0 |
| Release | 0 | 0 | 0 |

Signal and combined each include one additional recording-level Release
positive whose annotation windows differ from inference. Every other slot
remains unknown for that selected vibe, including songs labeled for another
vibe. These counts are reference coverage, not precision or false-positive rates.

Across all exact-window positive references, the following scores measure only
distance from the positive target 1. Signal and combined agree because direct
captured evidence overrides fallback on these recordings. Negative paired
differences mean smaller positive-only error; intervals resample connected
artist/album groups. This is a scale diagnostic, not a ranking comparison or
calibration test. A constant score of 1 would win it.

| Vibe | Positive recordings / groups | Metadata MAE | Signal and combined MAE | Paired difference, 95% interval |
| --- | ---: | ---: | ---: | --- |
| Relax | 14 / 14 | .381 | .535 | +.154 [.106, .199] |
| Focus | 18 / 17 | .434 | .838 | +.404 [.377, .434] |
| Cruise | 34 / 32 | .465 | .325 | −.140 [−.160, −.120] |
| Energize | 32 / 31 | .575 | .441 | −.134 [−.186, −.087] |
| Uplift | 18 / 14 | .507 | .557 | +.050 [.006, .081] |
| Release | 6 / 6 | .533 | .598 | +.066 [−.008, .139] |

## Radio, restrictions and incomplete captures

All three complete-capture conditions produce 24-song Radio episodes at each
dial setting. The midpoint's ordered identities are identical across arms.
Combined shares seven Chill and four Full-on recordings with metadata-only;
it shares sixteen and twenty-one respectively with signal-only. Distinct artist
credit strings range from 18 to 24 per episode; this is a variety diagnostic,
not the canonical artist grouping used for uncertainty.

Known model/metadata intensity coverage at Chill/midpoint/Full on is 24/22/24
for metadata, 23/7/23 for signal and 24/23/24 for combined, out of 24 each.
Means, transition deltas and provenance sources are in `comparison.json`.
They are model-derived diagnostics with no human intensity or sequence-quality
reference. Local song labels cover 3/6/0 metadata, 20/6/1 signal and 14/6/0
combined Radio slots; they do not label Radio episode quality.

Each arm passes 24 Mix genre include/avoid cases and three Radio genre-pin plus
fixed-exclusion cases: 81 cases total, no violations. All consumer annotation
read guards remain at zero and collaborative vectors remain unchanged.
Every served complete profile exactly matches its declared arm.

Restrictions can shorten outputs: restricted Mix cases return 31–48 total songs
per four-card set in each arm. Sixteen of 24 metadata/combined cases and fourteen
of 24 signal cases reach 48. Restricted Radio returns 16/11/16 songs at
Chill/midpoint/Full on in every arm. No restricted case is empty, and the report
does not claim those shorter lists met the unrestricted length target.

The first-incomplete-capture conditions also fill every card and episode.
Combined reproduces metadata-only's ordered outputs and profiles exactly.
Signal retains zero-support heuristic vibe fits and unknown intensity for all
72 Radio slots, without inventing neutral measurements. Continuing capture with
a previous usable measurement remains the separate #641 evidence described below.

Sparse and conflicting retained metadata are present in the actual outputs:
among 360 complete Mix/Radio slots, metadata, signal and combined respectively
contain 13, 86 and 49 slots with no known metadata dimensions, and 2, 0 and 2
slots with a conflicting dimension. They include 4, 1 and 3 originally partial
capture recordings counted by output occurrence. These are observed cohorts,
not a claim that every possible conflict or capture failure was simulated.

## Retained failures and operational interpretation

Round 1 stopped during the generic recovery exporter, which was republishing
over 111,000 historical recommendation files. Its stop request and copied files
remain. Round 2 instead copied and checked each referenced physical file once,
then backed up SQLite in the same read-only transaction. Export took 97.079 s.

Round 2's metadata arm failed its original 180-second publication budget while
staging candidate pools: 180.010 s wall, 160.420 s CPU, before consumer assembly.
The complete failed attempt took 266.717 s and peaked at 653,266,944 bytes RSS.
The failure report and private account remain intact.

The separately declared continuation uses 600 seconds per publication, 30 per
consumer operation and 1,800 per arm, with one numerical thread and one arm at a
time. All inputs and scoring rules remain identical. Completing this diagnostic
does not pass or erase the original publication limit. Process RSS includes
Python, research imports, export verification and retained-account overhead;
it is not browser memory or a per-operation allocation measurement.

Round 3 published metadata pools in 275.832 s, then hit the original 30-second
Radio episode budget. Its six Mix assemblies returned in 1.29–1.34 s, but their
ordered outputs had not yet been saved. Round 4's separately declared consumer
continuation retains 600 seconds per publication and 1,800 per arm, extends each
consumer diagnostic to 180 seconds, and saves outputs incrementally. It reuses
the exact metadata publication after copying the failed private database and
verifying identical profile hashes and publication heads. That reuse is recorded,
not counted as a new publication timing. Signal and combined build independently.
Reported arm wall time excludes the before/after input-integrity scans; phase
timings include the adapter and ordinary consumer work.

The timing environment uses Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1 and
threadpoolctl 3.7.0. Numerical libraries are limited to one thread, and arms run
sequentially. The machine is not reserved exclusively for this experiment.
Retune timing covers candidate construction and four direction plans, using the
existing benchmark's scope; HTTP handling, session persistence and remote
playlist writes are outside that measurement.

| Operation | Metadata | Signal | Combined |
| --- | ---: | ---: | ---: |
| Complete publication, wall / CPU seconds | 275.832 / 227.911, reused | 264.935 / 224.963 | 274.635 / 229.048 |
| Warm retune, three wall seconds | 55.592, 56.769, 55.144 | 48.774, 50.589, 48.267 | 48.603, 52.747, 49.058 |
| Complete Radio, Chill / midpoint / Full-on seconds | 51.036 / 7.118 / 52.331 | 42.745 / 6.969 / 44.697 | 44.143 / 7.137 / 47.324 |
| First-incomplete publication, wall / CPU seconds | Not applicable | 291.013 / 232.794 | 301.362 / 236.126 |
| Arm wall seconds, scope described above | 510.380 | 1134.768 | 1241.977 |

Warm Mix assembly is approximately 1.25–1.36 seconds. Each process reports
653,266,944 bytes peak RSS (623 MiB), including launcher/import/verification and
retained-account overhead; it is not a measured model allocation or browser RSS.
The slow candidate/direction work is linked to existing #633 for investigation,
without asserting it is the cause of that live launch failure.

## Evidence reused without repeating its campaign

- #424 was independently accepted at
  https://github.com/reubenfirmin/TuneConsole/issues/424#issuecomment-6512727819 .
  Its 32 input comparisons, eight controlled component-capture/resource cases,
  and 1,310-recording / 2,191-input preprocessing comparison establish their
  recorded pipeline facts. They do not certify changed specialist quality,
  laptop battery use or signed-in YouTube scheduling.
- #573 remains In review. Its bounded frozen-schema-102 cold result reached a
  compatible Cruise recording in 35.42 s; later consumer continuation could not
  establish an intact original runtime. That result is not carried forward as
  current whole-account, rendered-consumer or rollback acceptance. See
  https://github.com/reubenfirmin/TuneConsole/issues/573#issuecomment-6521809997 .
- #641's author replay used all 84 earlier actual captured songs through
  metadata, first-excerpt, complete-capture, adoption and rollback phases.
  Seventy-three complete songs changed Focus; eleven partial songs retained
  fallback. Its 243 selected checks include continuing updates and propagation.
  `FOCUS_PRODUCTIONIZATION.md` identifies exact receipts and limitations.
  Independent #641 acceptance remains pending; these are author observations.
- #586 is Done for evidence/intent/profile/policy display. This experiment does
  not reopen that display work or #584's cancelled listening campaign.

## Reproduction

Entry points are `./experiments/run.py output-evaluation` and
`./experiments/run.py output-evaluation-report`. The first requires the complete
frozen runtime via `--runtime-snapshot`. Plans, adapter revisions, manifests,
failed attempts and outputs are retained under
`experiments/audio_lab/local/output-evaluation-587-20261030/`.

The completed runs are `round-4/metadata-2026-10-30T202842.052827_0000`,
`round-4/signal-2026-10-30T203924.579054_0000` and
`round-4/combined-2026-10-30T205850.007099_0000`. Run each arm with:

```sh
./experiments/run.py --runtime-snapshot experiments/audio_lab/local/external-20261028/checkpoint-1/library-replay/runtime-20261030T195411.511565Z output-evaluation run --directory experiments/audio_lab/local/output-evaluation-587-20261030/round-4 --arm signal
```

Use `--arm combined` for the combined arm. The retained metadata continuation
used `--arm metadata --resume experiments/audio_lab/local/output-evaluation-587-20261030/round-3/metadata-2026-10-30T201447.082202_0000`.
Reproduce the comparison with:

```sh
./experiments/run.py output-evaluation-report --directory experiments/audio_lab/local/output-evaluation-587-20261030/round-4 --local experiments/audio_lab/local/output-evaluation-587-20261030/local-1 --run experiments/audio_lab/local/output-evaluation-587-20261030/round-4/metadata-2026-10-30T202842.052827_0000 --run experiments/audio_lab/local/output-evaluation-587-20261030/round-4/signal-2026-10-30T203924.579054_0000 --run experiments/audio_lab/local/output-evaluation-587-20261030/round-4/combined-2026-10-30T205850.007099_0000
```

Plan SHA256: `a7990b62c38ab3f2d9ebfe25d139fc664eb07175390a40c62cefbb7282c29cd7`.
Comparison SHA256: `2c4e2c94d7869d14b387c47860a830ef5f46e0b27bad0c274b81027b3b68d52c`.
Inference-audit SHA256: `2d8f69c6e6a57c89ee8aac78a47eb482ac895e660ce57ceb83dcb6a5031e0b27`.
`comparison-receipt.json` pins the reporter, local-label receipt and all three
arm receipts; those pin ordered outputs, profiles, restrictions and run reports.

Focused checks: `./experiments/run.py check --test test_output_evaluation.py`.
Ten pass in 0.89 s, covering evidence ablation, nonrecursive propagation,
partial-capture fallback, connected groups, paired intervals, unknown labels,
unknown intensity, clone file-owner isolation, ordered output comparisons and
rejection of mismatched served profiles. Exact recording identity is also
checked separately from capture-window identity, and legacy fallback cannot
excuse missing specialist routing on compatible audio. These controlled fixtures
support the harness; they do not substitute for the actual-account outputs.

## Disposition

The declared report is complete and ready for independent review under #587.
It establishes reproducible output differences, fallback behavior, restriction
preservation and operational costs. Whole-list benefit, unsuitable inclusions
and externally labeled output quality remain unidentified. No model is refitted
or selected here. #642 owns the shared-model retirement and fallback/score-scale
follow-up in a standalone hierarchy outside #136/#530 closeout; #573 and #641
retain their independent production acceptance. #405, #530 and #136 are In review
alongside their remaining children, reusing existing evidence rather than adding
another campaign. Independent acceptance is required before Done.
