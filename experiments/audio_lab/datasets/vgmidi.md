# VGMIDI

Source, rendered audio and annotations: [author repository](https://github.com/lucasnfe/vgmidi).

VGMIDI is a collection of video-game MIDI piano music with human time-varying
valence/arousal annotations on a −1 to 1 scale. The full labeled collection
describes 200 pieces and 204 phrase rows. The repository separately supplies
99 rendered WAV files;
these counts refer to different units.

TuneConsole uses 76 of those supplied renders that pass unique title/franchise,
duration and complete-listener joins. Targets average the original changing
annotations into equal-listener whole-piece means. Center-excerpt features with
whole-piece labels are weak supervision, rather than phrase-level classes or
temporally aligned annotations. New MIDI renders and commercial soundtrack
recordings are not substitutions for these rated files.

The acquired subset has five game-franchise groups. Composer identities are
absent, so franchise grouping cannot establish composer-independent evaluation.
Keep this rendered-piano source distinct from recorded-music collections.
