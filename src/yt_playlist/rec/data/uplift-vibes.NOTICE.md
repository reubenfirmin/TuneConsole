TuneConsole's Uplift consensus contains frozen ridge coefficients and preprocessing
statistics from #618. It distributes no source audio or per-song annotations.
The fixed blend uses Soundtracks (25%); DEAM, PMEmo, Jamendo human, Emotify,
ManyMusic and Memo2496 (12.5% each). ManyMusic divides its weight equally between
natural and generated recordings. The learned blend is not included.

Sources and retained terms:

- [DEAM](https://cvml.unige.ch/databases/DEAM/): 1,800 retained recordings,
  native valence. Retain the original publisher's terms.
- [PMEmo](https://github.com/HuiZhangDB/PMEmo): 766 retained choruses,
  native valence. Retain the original publisher's terms.
- [Soundtracks](https://osf.io/p6vkg/): 470 retained excerpts, native valence
  and happiness. Retain source terms; overlapping sets are not independent.
- [MTG-Jamendo](https://github.com/MTG/mtg-jamendo-dataset): 216 retained
  explicit human happy-vote targets. The dataset states noncommercial research
  terms; individual recordings retain their original licences.
- [Emotify](https://www2.projects.science.uu.nl/memotion/emotifydata/): 400
  supplied excerpts with native felt joyful-activation annotations. Retain
  the publisher's source terms.
- [ManyMusic-Stim v2](https://zenodo.org/records/23647077): 188 natural and
  95 generated recordings. The [publisher](https://manymusic.net/stim/)
  specifies CC BY-NC-SA 4.0 for generated audio, metadata and human ratings.
  Natural recordings retain their individual Jamendo licences.
- [Memo2496](https://figshare.com/articles/dataset/Memo2496/25827034): 63
  retained pilot recordings; the source record lists CC BY 4.0.

The research export's source-terms status is retained. This notice does not
establish distribution clearance for the derived artifact or relicense source
datasets under TuneConsole's MIT software license. The artifact records the
frozen source model and plan hashes. User adoption accepts the documented
generated-music regression; it does not change the original research result.
