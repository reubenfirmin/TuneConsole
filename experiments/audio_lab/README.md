# Audio experiments

Experiments that shaped the models. See the [dataset index](datasets/README.md)
for labels and sources. Evidence paths below are relative to this directory;
full results and frozen inputs live in ignored artifacts. Commands: `../run.py --help`.

**Local six-vibe fitting.** On 40 labeled tracks, hard-example fitting reached
26 acceptable choices but nested selection reached only 22. Alternative-label
losses did not help. Local-set optimization was unreliable, motivating external
native-label supervision. Evidence: `local/external-20261028/`.

**Native attributes and shared classification.** MusiCNN ridge predicted native
Emotify/PMEmo/Soundtracks labels better than constants. Calibrating Emotify heads
on 55 human-labeled songs beat embedding-only classification, 38/55 versus 34/55,
yielding `emotify-vibes.json`. The heuristic still ranked better.
Evidence: `local/external-20261028/`.

**Broader replacement search.** Thirteen sources, alternate learners, PCA,
multitask fitting and unlabeled consistency produced no broad replacement.
Per-vibe AP selection also lost to shared (.397/.516), motivating fixed native
attribute mappings evaluated on external targets. Evidence:
`local/external-20261028/checkpoint-1/`, `local/per-vibe-20261029/`.

**Cruise.** Equal PMEmo/DEAM valence-arousal experts, mapped to positive valence
and moderate arousal, beat shared on ManyMusic/Memo and became `cruise-vibes.json`.
Shared refits regressed other vibes: retain shared classification and separate
specialist ranking. Evidence: `local/mapped-vibes-20261029/common-target-1/`.

**Audio preparation.** Common-input browser/native inference matched, but
independent FFmpeg preparation changed Cruise scores by up to .116. Browser
ranking gains held on ManyMusic/Memo and failed on Soundtracks. Keep extraction
and aggregation fixed. Evidence: `local/preprocessing-quality-20261029/run-1/`.

**Focus.** Emotion proxies gave uncertain gains. Direct CAL500 Reading/Studying
ridge heads reached browser NDCG .499 versus shared .400. Their clipped average
became `focus-vibes.json`, despite missing the frozen margin over its predecessor.
Evidence: `local/focus-615-20261030/round-1/`.

**Release.** Adding power worsened aggression-based anger ranking (.534/.711).
The Jamendo aggression ridge became `release-vibes.json` by explicit adoption
with uncertain gain; aggression remains a catharsis proxy. An artist-extension
attempt acquired no new audio. Evidence: `local/release-614-20261030/round-2/`.

**Energize.** Activation, joy-inclusive consensus and exercise mapping failed
advancement gates. The no-joy arousal/energy/power consensus became
`energize-vibes.json` by explicit adoption accepting domain losses.
Evidence: `local/energize-616-20261030/round-1/`.

**Relax.** Fixed calmness, low tension/energy, tenderness and valence-arousal
consensus improved mean NDCG by .043 over prior embedding. Learned weights failed
transfer limits; fixed consensus became `relax-vibes.json`.
Evidence: `local/relax-617-20261030/round-1/`.

**Uplift.** Fixed cross-source positivity improved mean NDCG by .042 but regressed
generated music. It became `uplift-vibes.json` by explicit adoption accepting that
tradeoff. Arousal-conditioned sigmoid gating subsequently failed ranking and
domain limits and was discarded. Evidence: `local/uplift-618-20261030/round-1/`,
`local/uplift-gate-712-20261101/round-2/`.

**Focus controls and count likelihoods.** Emotion residuals and task-specific
slopes gave no reliable ranking gain. Listener models improved endorsement
prediction, not demonstrated music ranking. Count likelihoods improved while
ranking lost to ridge; retain direct heads. Evidence: `local/focus-controls-637-638-20261030/round-1/`,
`local/focus-counts-711-20261101/round-1/`.

**More training windows.** Using all 1,695 ManyMusic windows with total weight
one per recording failed native ranking and domain gates. Generated recordings
had only one window and provided an unchanged control. Retain one-window recipes.
Evidence: `local/many-windows-707-20261101/round-5/`.

**MERT representations.** Final-layer MERT and intermediate layers 5/6 failed
every mapped-vibe gate on 288 matched recordings. Native-target improvements
did not translate into suitability; retain MusiCNN.
Evidence: `local/encoder-comparison-708-20261101/round-3/`,
`local/mert-layers-809-20261101/round-2/`.

**Reserved Memo confirmation.** On 120 previously reserved recordings,
Cruise/Relax comparisons were inconclusive and Uplift/Energize hit regression
limits. No specialist upgrade was established; this reserve is now consumed.
Evidence: `local/memo-confirmation-710-20261101/round-1/`.

**Chroma.** Adding 72 CQT summaries looked promising on 32 reused CAL500 songs,
but the larger protected comparison gained only .016 NDCG with uncertain gain
and worse lift/MSE. Discard this recipe.
Evidence: `local/chroma-810-20261101/round-1/`, `local/focus-confirmation-815-20261101/round-1/`.

**Mid-level properties.** Seven human properties were predictable, but their
predicted bottlenecks worsened emotion errors and augmentation failed every
vibe gate. True properties helped some tasks but are unavailable at inference;
discard the predicted heads.
Evidence: `local/midlevel-811-20261101/pilot-1/`.

**Classical transfer.** WCMED Uplift rank transfer improved NDCG by .208;
Energize remained uncertain. Rank-transformed targets, unavailable processed
audio and unverified proxies prevented adoption; retain specialists.
Evidence: `local/wcmed-812-20261101/round-3/`.

**Mood regulation.** SiTunes initial mood predicted final mood; music/context
descriptors added no reliable gain. Unverified audio windows and no untreated
comparison prevented a music intervention model.
Evidence: `local/situnes-813-20261101/round-2/`.

**Whole-output ablation.** Metadata/signal/combined Mix and Radio comparisons
exposed coverage/scale gaps: combined Relax/Focus cards used no direct specialist
slots. Positive-only labels could not establish precision.
Evidence: `local/output-evaluation-587-20261030/round-4/`.

**Joint metadata pooling.** Gaussian artist/genre/album pooling, learned metadata
and richer descriptors improved measurement MAE by 14–16% but failed unseen
ranking checks. Correlated/conditional sources and fixed offsets did not resolve
the losses. Reject those campaigns. Evidence: `../../build/test-reports/implementation819-descriptors-development-20261106/`
and `../../build/test-reports/implementation819-conditional-source-development-20261106/`.

**Pooling evaluation and human transfer.** Ranking across separately calibrated
folds tested an ordering no served model produces. Corrected within-model
evaluation and product-format fits passed numerical gates, but reused human
checks retained retrieval losses. A sealed 30-recording comparison was mixed,
with no Uplift positives and no precision evidence; numerical fit alone did not
establish adoption. Evidence: `../../build/test-reports/implementation819-within-model-selection-20261106/`
and `../../build/test-reports/implementation819-product-human-campaign-20261106/`.
