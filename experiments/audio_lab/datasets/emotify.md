# Emotify and Emotify+

Emotify source and annotations: [publisher release](https://www2.projects.science.uu.nl/memotion/emotifydata/).
Audio: [emotifymusic.zip](https://www2.projects.science.uu.nl/memotion/emotifydata/emotifymusic.zip).
Emotify+ source: [Mendeley Data release, version 1](https://data.mendeley.com/datasets/wbk8zr9bd2/1).

Emotify contains 400 music excerpts across classical, rock, pop and electronic
music, with listener reports of **felt emotion**. Its nine GEMS categories are
amazement, solemnity, tenderness, nostalgia, calmness, power, joyful activation,
tension and sadness. Targets retain how often listeners select each emotion;
selection frequency and intensity conditional on selection are different values.
Genre and within-genre filename join audio to global annotation IDs.

TuneConsole uses all 400 supplied excerpts. Recovered audio tags identify 138
artist groups; remaining missing identities limit claims of artist independence.

Emotify+ adds **perceived-emotion** judgments on overlapping Emotify audio.
Its 3,031 CSV rows identify 399 files and 174 listeners. TuneConsole's strict
title/genre consistency checks retain 265 tracks and deduplicate repeated
listener responses. It shares recording and artist exclusions with Emotify;
additional listeners do not make the audio independent.
