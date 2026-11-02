# Human property pilot, #811

The metadata acquisition succeeded. Use all 360 explicitly identified
Soundtracks Set 1 excerpts and their seven new human property targets. Join
publisher source IDs 1–360 to the existing original files, not title guesses.
The full supplied excerpts carry both annotation sets. Reuse the verified
historical center-ten-second MusiCNN embedding as a bounded representation of
each excerpt; this does not change either label's interval to ten seconds.
No new encoder extraction or 1.18 GB archive is needed for this pilot.

Audit exact file hashes, existing feature provenance and label scales before
fitting. Merge existing Soundtracks identity groups, exact audio hashes and
normalized film/album names transitively. Blank names do not join unrelated
items. Artist/composer identities remain incomplete. Use five grouped outer
folds and three grouped inner folds, seed 8111101. Keep all 360 rows if at
least 64 rows / 12 components remain; otherwise stop without fitting. All
nonpilot Memo data remain excluded. Freeze audit, inputs, sources and this
document before model fitting; the acquired acquisition round is unchanged.

Properties use the seven literal publisher columns (including `atonality`,
whose name/direction is not silently corrected), native scale 1–10. Normalize
to [0,1] with (value-1)/9. The publisher's CSV contains values up to 10;
the [2019 bottleneck paper](https://archives.ismir.net/ismir2019/paper/000027.pdf)
explicitly confirms this scale. The initial 1–9 assumption failed preparation
before any fitting and was corrected here before freezing the pilot.
Emotion retains all eight Soundtracks native targets
and its existing documented normalization. No semantic property inversion.

Compare direct embedding (200), predicted-property bottleneck (7), concatenated
embedding/predicted properties (207), and human-property oracle (7). Oracle
test properties are unavailable to a deployed system and cannot advance.
Use constant or ridge alpha .1,1,10,100,1000 per target, training-only scaling
and clipping. For every emotion inner train/validation split and outer fit,
fit/tune the property model only on that training partition; generate its
training predictions by another three-fold component cross-fit, tuning each
auxiliary fit inside its own training data. Never let an emotion validation
component enter auxiliary fitting, normalization or auxiliary selection.
Native emotion selection also uses only inner validation MSE. Recompute the
entire auxiliary pipeline inside each inner context, not once before CV.

Report seven held-out property errors versus training means and eight emotion
errors, plus fixed Soundtracks mapping rankings/lift, 1,000-draw paired component
intervals and all domain limitations. Apply the familiar .02 NDCG/.01 lift,
positive NDCG interval and no .01 ranking/lift loss or .005 MSE increase gates
to each predicted-property method. This is one-domain development evidence,
not independent transfer or a six-specialist production comparison. Any
positive result needs further confirmation; oracle gains are not deployable.

Budget one hour CPU fitting/evaluation, four GiB RSS and 64 MiB model outputs.
Keep sources, heads, predictions, training-component traces and receipts in
experiments/. No production edits, global-suite lock or listening collection.
