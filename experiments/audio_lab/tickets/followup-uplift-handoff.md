Completed #618's bounded research, including the user-requested consensus comparison. Ready for independent review. **Recommendation: retain the current Uplift scorer.**

The fixed and learned blends improve Soundtracks and Memo, but both exceed the predeclared ManyMusic regression limits. Mean historical NDCG gains over the refitted DEAM anchor are +0.06274 and +0.04547; those favorable averages do not override the per-domain limits.

| Historical grouped NDCG | Shared | Refitted DEAM | Fixed consensus | Learned consensus |
| --- | ---: | ---: | ---: | ---: |
| Soundtracks, 470 rows | 0.64317 | 0.56594 | 0.70807 | 0.68375 |
| ManyMusic natural, 171 | 0.70656 | 0.81690 | 0.76473 | 0.77984 |
| ManyMusic generated, 95 | 0.91163 | 0.90528 | 0.87718 | 0.89985 |
| Memo, 63 | 0.91971 | 0.88419 | 0.97042 | 0.92403 |

Fixed versus anchor Soundtracks gain is +0.14213, paired 95% interval [+0.09480,+0.26168]. Natural ManyMusic loses 0.05217, interval [−0.11278,+0.01638]; its top-list lift falls 0.02815. These point losses violate the conservative frozen rule even though their intervals include zero. Browser inputs and omitted-corpus comparisons preserve the tradeoff. Without ManyMusic training, fixed/learned natural NDCG loses 0.06038/0.03041 versus the anchor. All exact prior DEAM, refreshed, named PMEmo and shared/browser controls remain in the numerical report.

The label audit covers all 3,998 source rows across DEAM, PMEmo, Soundtracks, Jamendo human, Emotify, both ManyMusic strata and Memo. Separate native targets, joint identity exclusions and nested cross-fitting prevent fabricated co-annotations or validation leakage. Learned selection gives each corpus equal weight and each ManyMusic stratum half a corpus. No inference-time dataset identity or high-energy gate is used.

Soundtracks-native valence MSE is 0.02487 versus transferred DEAM's 0.06414. A diagnostic mapping from other observed attributes to the fixed happiness/valence reference reaches NDCG 0.95547; audio-predicted attributes reach 0.75073. Separately, observed valence predicts happiness ranking at 0.65470 versus 0.49695 for predicted valence. This supports both a prediction/domain mismatch and an incomplete semantic bridge, without proving a unique cause. One gentle-happy and zero positive-forceful Soundtracks excerpts leave those coverage claims unresolved.

The refreshed read-only local set has 84 usable songs, 232 captures and 23 Uplift positives; no identities or labels changed since #570. These are supplementary positive-only checks. All external populations were previously inspected and are not fresh confirmation. Joint folds differ from #580, so unchanged controls have different fold/top-list summaries; #580's original retain result is not rewritten.

Evidence in the shared workspace:

- `experiments/audio_lab/UPLIFT_CONSENSUS_PLAN.md`
- `experiments/audio_lab/UPLIFT_LABEL_AUDIT.md`
- `experiments/audio_lab/UPLIFT_CONSENSUS_RESULTS.md`
- `experiments/audio_lab/local/uplift-618-20261030/round-1/`: archived code/input hashes, nested traces, full and held-out exports, individual predictions, all folds, calibration, 1,000-draw paired component intervals, contrasts, temporal comparisons and decision failures.
- Plan SHA256: `2bf73a9f8f1360f50173f2cb3311dd1f31f9584a0d51054749704e5f28bb5b66`.
- Evaluation receipt SHA256: `2e05a7d63d4d1e4aab1e116aa5d5d52d84dd973a361ee913c7c042d40e25fb8b`.

Validation:

```sh
./experiments/run.py uplift verify
./experiments/run.py check --test test_uplift_consensus.py --test test_uplift_replay.py
```

Receipt verification passed; **8 checks passed in 34.10 seconds**, including independent arithmetic replay of every held-out historical/browser prediction and actual-cohort exclusion checks. Research export: 148,730 bytes; maximum warm three-excerpt inference 0.778 ms; peak RSS 653,266,944 bytes; maximum full-export local replay difference zero. Python 3.14.8 / NumPy 2.4.6 / SciPy 1.18.1 / threadpoolctl 3.7.0, one numerical thread. Experiments did not use the global-suite lock; no full suite was run.

No production change, commit or push. Source terms remain explicit and unresolved distribution status is preserved. These are author evidence and focused checks; the reviewer still owns independent acceptance of the declared research scope.
