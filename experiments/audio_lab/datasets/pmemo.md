# PMEmo

The supplied release has 767 choruses with static and changing arousal/valence
annotations, joined by music ID. Labels apply to the chorus, not arbitrary song
sections. Connected artist credits yield 267 groups, replacing the earlier
398 primary-artist groups. Later protected full fits retain 766 rows; the
common-target Cruise pilot retained 762 after destination exclusions.

Oct 28–29: grouped embedding MAE .08597 versus mean .14853 establishes useful
native VA prediction. Static and temporally aligned supervision were compared;
neither justified a broad shared-model replacement. PMEmo's source-selected
acoustic activation transferred poorly to Soundtracks (NDCG .5992 versus the
frozen embedding comparator's .8039); discard a general acoustic-only preference.

The duplicate-free unlabeled consistency, PCA/whitening and shared-task studies
did not establish a six-vibe gain. On 12 compatible local unlabeled recordings,
PMEmo neighborhood/soft-target selection chose zero local weight. Stored
predictions are not independent human evidence.

PMEmo supplies the ridge expert in [Cruise](../models/cruise.md) and native VA
heads in [Uplift](../models/uplift.md) and [Energize](../models/energize.md).
Evidence: `local/external-20261028/`, including `checkpoint-1/` and the original
`unlabeled-consistency/`; `local/mapped-vibes-20261029/common-target-1/`.
Source: [publisher repository](https://github.com/HuiZhangDB/PMEmo).
