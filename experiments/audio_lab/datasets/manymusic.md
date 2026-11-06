# ManyMusic-Stim v2

The acquired human/audio intersection contains 283 recordings: 188 natural
Jamendo and 95 generated Suno tracks, with 1,695 rated thirty-second windows.
Selected ZIP members have CRC and recorded SHA256 checks; full-archive MD5 is
unverified. Preserve signed VA labels, listener IDs and interval positions.
Average repeated responses within listener/window before averaging listeners:
112 repeats include 22 conflicting sets. Group natural artists and generated
prompts; keep the two strata separate. Curation used MusiCNN, limiting independence.

| Date / experiment | Result and decision |
| --- | --- |
| Oct 29, transfer pilot | One target-independent identity-hashed window per recording, three consecutive ten-second chunks. Dimensional Cruise transferred better than the shared checkpoint; Memo supplied a distinct comparison. |
| Oct 29, interval repair | Binary timestamp equality falsely rejected 21 complete windows. Retained original results/cache lineage and completed all planned intervals without padding. |
| Oct 30, consensus | Supports fixed VA specialists. Uplift's generated-music regression and Energize's domain losses remain accepted product tradeoffs, not passing research results. |
| Nov 1, #707 all-window training | All 1,695 windows / 3,000 responses accounted for; each recording totals weight one. Native ranking gains/domain gates failed. Retain the one-window procedures. Generated music has one window per recording and is the unchanged negative control. |

Matched later evaluations use 171 natural rows under their inherited exposure
mask; the original transfer pilot's overlap-excluded slice used 178, and the
shared-refresh comparison used 186. Do not mix these populations or scores.

Natural recordings retain individual Jamendo terms; generated audio, metadata
and ratings retain publisher CC BY-NC-SA 4.0. All 188 natural attribution joins
were recovered. Source: [v2 release](https://zenodo.org/records/23647077).
Evidence: `local/external-20261028/checkpoint-1/new-corpora/` and
`local/many-windows-707-20261101/round-5/`.
Protocol: [all windows](../MANY_WINDOWS_PLAN.md); models link their own mixtures.
