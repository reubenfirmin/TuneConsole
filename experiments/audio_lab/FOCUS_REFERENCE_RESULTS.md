# Focus confirmation-reference audit (#576)

30 October 2026. The bounded search acquired two relevant annotation releases
and compared their recording descriptions with eleven existing supplied-audio
feature inventories. It established **no verified new recording-and-window
confirmation set**. The acquired labels and candidate joins remain available for
future identity work; they are not used to select or evaluate this round's heads.

## Moods and activities

The [publisher's release](https://reshare.ukdataservice.ac.uk/852024/) supplies
5,129 track records, 28,550 listened-example tagging rows and 8,681 nomination
rows. Its README distinguishes these from mood-word/activity associations and
repeated mood consistency checks. The native `intellectual` activity includes
studying, reading, writing and desk work. It is a broader construct than this
experiment's equal Reading/Studying reference.

The 1,487,844-byte ZIP was downloaded and its members passed ZIP CRC on reading.
SHA256: `9437ec121b1ca491354b886bc768badced313be63ff5210b9820a19d15ee7f40`.
The README is also retained with acquisition metadata. No audio is supplied in
the archive. Original 7digital track, artist and release IDs remain in the data.

Exact normalized artist/title joins identify **209 track records** with existing
audio candidates; all have listened-example tags. Repeated answers by one
listener are averaged before computing listener fractions. These are candidate
text joins, not verified recording/version or excerpt joins. The archive does
not supply tagged excerpt positions, and the existing files do not establish
that they contain the same 7digital version and rated clip. Consequently none is
scored as new confirmation. Mood-word, nomination and consistency-check rows
are not substituted for independent listened-example labels.

## Help Me study!

The [publisher](https://zenodo.org/records/10085104) provides experience-sampling
data, a Spotify-feature table and a codebook. All three acquired spreadsheets
match the published MD5 checksums; their SHA256 values and exact URLs are saved.

The raw sheet has 280 data rows and identifies tracks at several prompts in each
episode. The bounded join found **453 unique nonempty artist/title pairs**,
**13 unique text matches**, and **31 matched episode/track fields**. No audio is
supplied and no exact recording/window equality is established.

The codebook includes perceived concentration and distraction effects, but
those are episode-level responses that may cover multiple songs. The prompt
track identifiers do not justify attributing an episode's outcome to every
track. Study participation, reasons for playing music and Spotify audio-feature
estimates are not replacement human suitability labels. These data therefore
provide no automatically attributable confirmation rows for the current model.

## Other leads and limits

[music&reading:Data1](https://zenodo.org/records/17042766) lists two fNIRS
spreadsheets, with no supplied audio or per-recording suitability release. The
previous cognitive-performance and Music in Use audits remain in
[DATASET_PLAN.md](DATASET_PLAN.md); a fresh OSF page request for the distraction
lead remained inaccessible. No access requests, user-listening collection or
expensive audio recovery were started.

The current primary evidence remains CAL500's original listener fractions.
Previously inspected CAL500 folds are reused-corpus evidence. The read-only
local refresh contains 84 labeled recordings, 18 with an explicit Focus label;
none is a new identity since #570. Missing local Focus labels are unknown.

## Reproduction

```text
./experiments/run.py focus-references acquire
./experiments/run.py focus-references inspect
./experiments/run.py focus-references audit
./experiments/run.py individual-local
```

Acquisition receipts, original files, workbook definitions, joined candidates,
feature-inventory hashes and the complete audit are under
`local/datasets/discovery/focus-576/`. The local export, content-addressed capture
copies and receipt are under `local/individual-20261030/local-1/`. The exporter
uses read-only SQL and does not open a Store, migrate or write to the account.
These are acquisition/identity findings, not model-quality measurements.
