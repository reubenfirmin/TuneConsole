# Focus chroma confirmation, #815

**Retain the incumbent Focus model. Do not productionize this chroma recipe.**
The larger, protected full-recipe comparison fails the frozen improvement
checks. The search also establishes no independent rankable human activity
confirmation set. These findings do not support starting browser or production
integration. Other specialists remain outside this experiment.

## Larger development comparison

Frozen [protocol](FOCUS_CONFIRMATION_PLAN.md), before new extraction or fitting:
`local/focus-confirmation-815-20261101/round-1`, plan SHA256
`8c9f0130668dbf2e7ec33e2c353d7bb3fddbd7818e2f74e04478557a74890656`.
494 recordings / 483 identity components; original #615 outer groups and nested
selection, with 450 common comparison recordings / 443 components as primary.
All are previously inspected CAL500 development data.

The 200-dimensional MusiCNN control uses the **complete incumbent direct-head
recipe**, refitted with held-out components excluded from both inner selection
and outer fitting. Its predictions reproduce all five accepted #615 historical
direct-head packs within 1e-12. The installed full heads exactly match the
accepted export, but have seen these songs; their replay is descriptive only.
No auxiliary models or nonpilot Memo recordings enter fitting or selection.

Primary common450 results; NDCG/lift and mapped MSE average the five outer folds.
Native MSE below averages recordings. Focus remains the fixed equal average of
Reading and Studying, with no outcome-dependent remapping.

| Metric | Protected incumbent recipe | Chroma alone | MusiCNN + chroma |
| --- | ---: | ---: | ---: |
| Focus NDCG | .497920 | .393279 | .514290 |
| Focus top-list lift | .114960 | .065699 | .107127 |
| Focus MSE | .018854 | .021361 | .018977 |
| Reading native MSE | .025541 | .027844 | .025636 |
| Studying native MSE | .033012 | .035941 | .033392 |

Combined minus protected incumbent, 1,000 paired component draws:

| Metric | Difference | Conditional 95% interval |
| --- | ---: | --- |
| Focus NDCG | +.016371 | [-.038792, .065672] |
| Focus top-list lift | -.007833 | [-.026675, .025846] |
| Focus MSE | +.000123 | [-.000152, .000414] |
| Reading native MSE | +.000095 | [-.000388, .000569] |
| Studying native MSE | +.000380 | [-.000055, .000814] |

The NDCG gain misses the frozen +.02 threshold, lift falls rather than gaining
.01, and the NDCG lower interval bound is negative. Native errors stay within
the permissive .01 regression cap, but neither improves. Reading NDCG changes
by -.009953 and Studying by -.035775; both intervals include zero. Chroma alone
performs worse. No candidate is substituted after seeing these results.

Focus NDCG changes across the five common450 folds are -.007889, +.059174,
-.003697, +.059511 and -.025246. Calibration does not reveal a useful gain:
422 predictions lie below .2, with incumbent/combined means .095456/.095853
against observed .092980; the other 28 have means .232847/.231971 against
.239583. No predictions reach .4. Native calibration bins and every fold's
ranking, lift and loss are retained in `report.json`.

The exact exposed full incumbent gives NDCG .555985 and lift .137127. Combined
is -.041695/-.030000 lower, failing the additional frozen exposed-replay checks.
This comparison is biased by training exposure; the protected comparison's
failed gain and uncertainty checks already suffice to retain the incumbent.

## Sensitivity and limits

All494: NDCG .483757 to .513488, difference +.029731
[-.039980, .064160]; lift +.005833 [-.030000, .028333].
Outside the original chroma pilot's components: 462 recordings / 451 groups,
NDCG .505432 to .518711, difference +.013279 [-.046727, .063483]; lift -.004500.
Studying MSE rises by .000621 [.000200, .001036] in that latter diagnostic.
These subsets were fixed before fitting; they remain development exposed.
They do not recover a reliable practical gain or establish independent success.

The original 32-track gain motivated this test; it does not repeat as reliable
improvement in the larger comparison against the full incumbent recipe. This
does not prove harmonic features can never help. It rejects deployment of the
current frozen recipe on the evidence available. Reusing these folds for more
feature tuning will not produce independent confirmation.

Labels are original whole-song checkbox fractions evaluated on ten-second
excerpts, not measured effects on concentration. The preserved raw support
audit covers 1,677 responses and no unknown activity values; omitted checkboxes
remain unselected. Most listener identities are unavailable. Bootstrap intervals
condition on folds and fitted models, and do not correct for prior exploration,
repeated listeners or domain shift.

## Independent reference recovery

Original downloads, failure receipts and source audit are retained in
`local/focus-confirmation-815-20261101/sources/` and copied into the frozen round.

- [HumMusQA release](https://zenodo.org/records/18462524): acquired all 320 human
  questions and 108-track metadata; both publisher MD5s pass. All 17 Functional
  Context questions and all three text-term matches were inspected. One answer
  says studying/working: Celestial Aeon Project's *Atmosphere*, Jamendo 26435,
  question index 225, rated excerpt 00:00–01:00. Its artist occurs in prior
  ManyMusic development data (*Inspiring*, `manymusic:25250:32.13334:30.0`).
  Nineteen prior inventories identify identity/artist overlap for 47 tracks.
  The other 61 have no direct Reading/Studying suitability labels. Unknown
  activities, distractors and descriptions of a focused snare are not negatives
  or new listener votes. No audio was downloaded to score this ineligible set.
- [PepMusic paper](https://doi.org/10.1140/epjds/s13688-020-0221-9) and
  [project](https://social-dynamics.net/pepmusic/): project requires a data-request
  form; none submitted. Human ratings bundle studying with dining, commuting,
  driving and breakfast. Its larger playlist-derived corpus is weak activity
  association, not separately rated Reading/Studying suitability.
- [Music in Use](https://osf.io/n3zmp/): public OSF file API returned 401. The
  transcription-derail task is also distinct from native suitability votes.
- [Brain-focus repository](https://github.com/arctop/Measuring-And-Modeling-The-Effect-of-Audio-On-Human-Focus-In-Everyday-Environments-Using-BCI):
  resolved the paper's short link and inspected the complete five-file Git tree,
  SHA `43a5c5e76a2b7c65490ee0d88347cf4fcd44ddb9`. README describes 51 participants'
  session-level decoded focus and self-reports across four stream conditions.
  No stimulus audio is supplied in that tree. Model predictions or session
  ratings cannot become per-recording human suitability labels.
- [Previously acquired activity references](FOCUS_REFERENCE_RESULTS.md): exact
  original 7digital recording and rated-window matches remain unverified. One
  bounded public preview probe returned 400; this does not prove all recovery
  impossible. Help Me study! still has episode outcomes covering multiple tracks.

Independent confirmation therefore remains missing. A useful future route needs
a varied, independently labeled activity corpus with verified audio and identity,
or a genuinely different hypothesis under a new frozen protocol. The current
results do not justify another productionization subtask or browser port.

## Resources and checks

494 exact original intervals extracted in 90.54 seconds, 223.34 MiB worker RSS.
All 32 shared chroma vectors replay with **zero error**, and their 16-kHz PCM
hashes match #810. Feature artifact 167,314 bytes. First-window computation 3.95
seconds with an existing compilation cache; subsequent median/p95 .052/.057
seconds. This is a Python extraction measurement, not browser feasibility or
fresh-cache cold-start evidence. Pinned Python 3.12.13, NumPy 1.26.4, SciPy
1.14.1, Librosa .11; full dependency lock retained. Temporary PCM is cleaned.

Fit 1.11 seconds, evaluation 18.43 seconds, main-process peak 534.71 MiB;
five outer model packs total 451,426 bytes. Fitting/evaluation runtime: Python
3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0, one numerical thread.
These packs are research artifacts.

```text
./experiments/run.py focus-confirmation acquire
./experiments/run.py focus-confirmation audit-sources
./experiments/run.py focus-confirmation freeze
./experiments/run.py focus-confirmation extract
./experiments/run.py focus-confirmation run
./experiments/run.py focus-confirmation verify
./experiments/run.py investigate focus_confirmation_815.py
```

Verification: **seven receipts**, exact frozen source/input integrity; **3 focused
actual-data checks passed in 1.73 seconds**. Checks cover source eligibility and
publisher checksums, raw response support, all group boundaries, unchanged
accepted incumbent predictions, train-only imputation/scaling, independent
exported-form inference, PCM/feature parity, saved errors and decision evidence.
No full suite/global lock, production edits, commit or push. Independent review
still owns acceptance of the retain decision and the documented reference gap.
