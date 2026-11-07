# EMMA — Emotion-to-Music Mapping Atlas

Source, preview player and annotation export: [EMMA database](https://musemap-tools.uibk.ac.at/emma/).
Definition and original study: [EMMA paper](https://doi.org/10.3758/s13428-024-02336-0).

EMMA is a database of emotionally evocative classical, pop and hip-hop/rap
excerpts rated for music-evoked emotions using the Geneva Emotional Music Scale
(GEMS). The public export includes emotion intensities, rater counts and
agreement, plus liking and familiarity judgments.

TuneConsole uses 333 publisher previews matched to original audio codes by
strict artist/title identity: 97 classical, 136 pop and 100 hip-hop/rap clips.
The selected GEMS-45 export supplies nine 0–100 targets: wonder, transcendence,
nostalgia, tenderness, peacefulness, joy, power, tension and sadness. Liking and
familiarity are excluded from emotion targets. Preview files are served at
`https://musemap-tools.uibk.ac.at/emma/songs/{code}.mp3`, where `code` is the
publisher's audio identifier.

The acquired previews last about 15 seconds, while the original rated excerpts
last 30–60 seconds. Precise preview alignment is unverified. This is weak
supervision on matched previews; it does not reproduce the original EMMA
benchmark. The current public CSV is also distinct from the original study
tables. Malformed and unmatched export rows are excluded.
