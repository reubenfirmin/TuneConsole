# Audio pipeline and consumer evaluation log

This filename is retained because an immutable preprocessing report links to
it. Model derivations now live in [model provenance](README.md#model-provenance).
These dated measurements summarize historical experiments, not current release
or whole-account acceptance. No additional user listening sessions, pairwise
intensity judgments or whole-mix ratings are required (#584, user's decision).

## Oct 29, 2026: browser input and resources (#424)

24 supplied recordings / 32 ten-second inputs compare the production browser
worker to native C++ inference on **identical browser PCM and patch policy**.
Maximum embedding/tag/mood errors: .0000209014 / .0000010282 / .0000015994;
all numerical and local resource limits passed. Eight controlled MV3 captures
also passed lifecycle, numerical and budget checks, including one-core affinity.
Capture plus analysis took 20.99–25.24 s, CPU 10.67–12.17 core-seconds, browser
peak PSS 491.3–509.4 MiB. This is one desktop, not laptop/battery evidence or
signed-in playback scheduling. Capture cleanup and advancing playback were
observed; subjective audibility was not measured.

Decoder equivalence **did not pass**. Independent FFmpeg downmix/resampling
changed model scores; arithmetic averaging alone is not an accepted repair.
Memo 1096 retained Cruise difference .11582 and embedding difference 2.35434.
Four saved capture containers reproduced browser PCM bit for bit, isolating
independent preparation as the remaining difference. Common-input inference
parity does not establish equivalence of complete training/serving pipelines.

The [hashed preprocessing report](PREPROCESSING_QUALITY_424.md) covers 1,310
recordings / 2,191 inputs, five cohorts and twenty supported contrasts.
Cruise browser NDCG beats shared on natural/generated ManyMusic and Memo
(.8393/.6616, .8697/.6988, .8841/.7845), but loses on supplementary Soundtracks
(.6963/.8128). All twenty preparation-contrast intervals include zero, which
does not establish equivalence. A changed input contract needs versioned features
and renewed paired external-label evaluation; preserve historical caches.

Evidence: `local/evaluation-424-20261029/run-2/` (run 1 failed before inference)
and the preprocessing report's frozen paths. Reproduce with
`./experiments/run.py audio-evaluation --help`, `audio-evaluation-report`,
`preprocessing-quality`, `preprocessing-quality-report`. Exact artifact/licence
findings remain in frozen reports and distributed notices; numerical acceptance
does not confer source rights.

## Oct 30: complete output ablations (#587)

One isolated account snapshot: 14,332 recordings, 10,254 retained profiles,
2,607 provider-measured recordings, 124 complete captures. Of those captures,
83 use compatible embeddings/all six specialists and 41 use legacy audio
heuristics. All complete profiles replay exactly. Pending preparation and this
frozen runtime prevent a fresh-account or current-build acceptance claim.

Metadata-only, signal-only and combined arms produce full Mix cards and Radio
episodes with zero violations across 81 explicit restriction cases. Combined
first-incomplete-capture outputs reproduce metadata-only exactly; unknown
intensity remains unknown. Combined Relax/Focus cards contain no directly
specialist-scored slots; all 25 direct-audio Release slots use legacy heuristics.
This exposes score-scale/coverage questions, not demonstrated unsuitable songs.

The 85 local positive-label recordings (73 exact inference-window matches)
cannot establish false positives, whole-list precision or musical benefit.
None of 5,725 external reference rows has a verified local recording/window
crosswalk. Predictions and propagation are never evaluation truth.

Original 180-second publication and 30-second Radio budgets failed. Separately
declared continuations completed at 265–276 seconds publication, roughly
48–57 seconds warm retune and 1.25–1.36 seconds warm Mix assembly. Preserve
the failures; larger diagnostic limits do not pass the original limits. Earlier
#530's activity-semantic warm retune also failed at 30 seconds; its restriction
continuation did not erase that failure. Whole-account/rendered consumer and
live scheduling evidence remain separate from coefficient replays.

Evidence: `local/output-evaluation-587-20261030/round-4/` and `local-1/`, with
rounds 1–3 and the exact runtime snapshot preserved. Plan SHA256:
`a7990b62c38ab3f2d9ebfe25d139fc664eb07175390a40c62cefbb7282c29cd7`.
Use `./experiments/run.py output-evaluation-report --help`; the comparison
receipt pins all arm outputs, input profiles, inference audit and local labels.

## Nov 1: shared runtime extraction (#702)

Moving common helpers to `audio_vibe_runtime` preserved exact nested outputs
for 84 real recordings (11 partial), 159 composed predictions and 477 research
routing cases. Installed-wheel replay retained six specialists and seven exact
artifact identities. Evidence: `local/specialist-cleanup-702-20261101/`;
`./experiments/run.py investigate specialist_cleanup_702.py -k test_replay -s`.
This establishes inference/code parity, with no new fitting or quality claim.
