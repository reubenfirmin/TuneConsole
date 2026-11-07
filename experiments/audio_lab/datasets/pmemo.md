# PMEmo — music emotion dataset with physiological signals

Source, documentation and download links: [publisher repository](https://github.com/HuiZhangDB/PMEmo).

PMEmo contains popular-music choruses with static and time-varying valence/arousal
annotations, recording metadata and physiological measurements. Songs were
selected from international popular charts. The originally published collection
describes 794 songs; the supplied archive used by TuneConsole has 767 choruses.
Music IDs join the audio, metadata and separate arousal/valence tables. The
acquired static tables supply normalized 0–1 mean ratings.

TuneConsole uses the native static and changing emotion annotations with the
supplied chorus audio, rather than the repository's visualization summary or
physiological signals. Artist connections across all credited collaborators
produce 267 groups. Protected full fits retain 766 recordings; the separate
common-target Cruise comparison retains 762 after destination exclusions.

The annotation unit is the chorus. Its ratings cannot be assigned to arbitrary
sections of the complete song. Repeated recordings and shared artist credits
must remain together across evaluation splits.
