# ManyMusic-Stim v2

Source and archive: [ManyMusic Stimuli, version 2](https://zenodo.org/records/23647077).
Dataset description: [publisher's stimulus page](https://manymusic.net/stim/).
Human ratings: [publisher's behavioral validation](https://manymusic.net/stim/plots_bhv.html).

ManyMusic supplies natural and generated music stimuli for emotion experiments.
TuneConsole's acquired human-rating/audio intersection contains 283 recordings:
188 natural Jamendo tracks and 95 generated Suno tracks. They supply 1,695 rated
thirty-second windows and 3,000 responses. Valence and arousal use signed
coordinates; listener IDs and window start positions remain part of each label.
These counts describe the rated subset, rather than every file in the archive.

Repeated responses are averaged within listener/window before averaging across
listeners. The data includes 112 repeated-response sets, 22 with conflicts.
Natural recordings are grouped by artist and generated recordings by prompt;
the two strata remain separate. The selected training procedure uses one
identity-selected window per recording, represented by three ten-second chunks.

Curation used MusiCNN, limiting independence from that encoder. Natural audio
retains individual Jamendo terms; generated audio, metadata and ratings use the
publisher's CC BY-NC-SA 4.0 terms. Selective ZIP acquisition verifies member
CRC/SHA256, rather than the whole archive's MD5.
