The enlarged-corpus comparison is complete for review. The result supports
**Cruise individual model plus the current validated shared checkpoint for the
other five vibes**. Neither refreshed shared control consistently improves the
other vibes. No product default or source was changed.

Implementation and full evidence:
`experiments/audio_lab_tools/cruise_baselines.py`, `test_cruise_baselines.py`,
`experiments/audio_lab/CRUISE_BASELINE_RESULTS.md`, and
`experiments/audio_lab/local/mapped-vibes-20261029/cruise-baselines-1/`.

The read-only export freezes **84 compatible human-labeled recordings**.
Connected identity groups reserve all eight new recording IDs before fitting:
76 development / 8 reserved. There are no changed labels on the original gold
IDs and no recorded reserved matches in the audited protected, gold, silver,
teacher-training or fitted Cruise native cohorts. A separate identity-only
post-evaluation audit also finds zero reserved matches in the original shared
Emotify native training cohort. Missing identities, aliases and unknown upstream
or other historical weak-training exposure remain limits; this is not a
representative independent listening panel.

Both controls retain the shipped ridge architecture. One refreshes calibration
only; the other refreshes native Emotify plus calibration, excluding the union
of local and external evaluation identities and retaining 260 native songs.
Three grouped inner folds select regularization. Five outer development folds
diagnose calibration selection. The final penalties are native 100 and
calibration 10. Both exported models match the product scalar inference on all
76 development embeddings within absolute tolerance 1e-12. Reserved outcomes
are not read by the fit stage.

External comparison covers **20 supported dataset/vibe pairs** across five
cohorts, all shared controls on identical known-training-disjoint rows. Cruise
NDCG for checkpoint / calibration refresh / both refresh / individual:

| Audio-backed cohort | Rows | Checkpoint | Calibration | Both | Cruise |
| --- | ---: | ---: | ---: | ---: | ---: |
| Natural ManyMusic | 186 | .6669 | .6544 | .6569 | **.8358** |
| Generated ManyMusic | 95 | .7022 | .6916 | .6646 | **.8943** |
| Memo2496 pilot | 63 | .8156 | .8100 | .7947 | **.8646** |

Cruise gain versus checkpoint, with conditional paired 95% intervals:
+0.1689 [0.0865, 0.2274], +0.1921 [0.0953, 0.2753], and
+0.0491 [-0.0444, 0.1439]. Intervals against both refreshed controls also stay
positive on the two ManyMusic cohorts and cross zero on Memo2496. These are
1,000 fixed-model paired identity-group draws within the existing folds, without
multiple-comparison correction. The cohorts were previously inspected.

Shared refresh improves Soundtracks Release (.6217 to .6688/.6619), but several
other scores regress: CAL500 direct Focus .4279 to .2614/.3306, soundtrack
Energize .8489 to .7888/.7754, generated Uplift .9219 to .8658/.8437. The full
report includes every declared supported mapping, absolute-score errors,
coverage and intervals. There is no automatic shared-model promotion or
post-hoc insertion of additional individual models.

Only after artifacts and external comparisons were fixed were the eight
reserved local outcomes scored. All three shared models have 6/8 acceptable
top choices. Cruise AP is .4167 for each shared model and 1.0000 for the
individual model; its two positive tracks rank first. This tiny slice is
supplementary. No Focus positives are present. Other five scores remain
unchanged, and mixed-scale scores are never used for overall argmax.

Commands: `./experiments/run.py cruise-baselines freeze`, `fit`, `evaluate`,
`verify`; `./experiments/run.py cruise-audit` for the later identity-only check.
Immutable plan/input/code/model/output manifests and receipts are preserved.
`./experiments/run.py check`: **118 passed in 4.10 seconds**, normal machine
lock. Research syntax and `git diff --check -- experiments` passed. Runtime:
Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0.

#571 has the compact artifact/routing prototype in review. Remaining production
work is #572–574: score semantics and consumer separation, bounded actual-library
rescoring/propagation, shadow/listening and release acceptance. The eight local
outcomes are now consumed and must not be represented as untouched in later
tuning. Only the review agent should accept this ticket as Done.
