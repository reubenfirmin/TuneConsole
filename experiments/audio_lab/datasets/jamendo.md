# MTG-Jamendo human judgments

Source and audio download instructions: [MTG-Jamendo dataset](https://github.com/MTG/mtg-jamendo-dataset).
Annotations used: [human classification judgments at the recorded revision](https://github.com/MTG/mtg-jamendo-dataset/tree/cafd8e20c265ed84f1e61f1c875327971f43a62f/derived/music-classification-annotations).
Terms: [publisher's licence section](https://github.com/MTG/mtg-jamendo-dataset#license).

MTG-Jamendo is a music auto-tagging dataset. Its mood/theme subset has 18,486
tracks with uploader tags. A separate derived annotation collection contains
explicit human yes/no judgments, including relaxed, happy, aggressive, party,
sad, acoustic and electronic. TuneConsole uses the fraction of positive human
votes for each task, preserving disagreement. An absent uploader tag is unknown.

The acquired subset contains 216 recordings from 104 artists, with complete
judgments for all seven tasks. Audio comes from official low-quality shards
00, 07, 23 and 53; archives and selected tracks match the official SHA256 lists.
The protected Release fit uses 187 recordings after transfer exclusions.

The derived judgments reuse the original split-0 test pool. Training on them
requires new artist grouping; split 0 is no longer an untouched evaluation set.
Aggression describes the music, rather than anger relief from listening.
The repository specifies noncommercial research/academic dataset terms and
individual track licences.
