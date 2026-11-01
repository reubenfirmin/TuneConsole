# Mix choice precision and group lifetime (#688)

A choice describes the displayed lane/label/description and the set of raw
genre labels carried by that rendered card. Capture uses the card's `genres`
value, including its artist metadata fallback. Equal sets share an integer
descriptor; membership is checked when reusing a digest. Raw labels are not
replaced by today's taxonomy keys or families.

Menu identity, episode, option identity/order, visibility, choice/action IDs,
event and observation times, withdrawal targets, scores/prediction receipts and
assignment receipts keep their existing meanings and lifetimes. Mix evidence
does not own recordings, recipes, track features, group versions, ancestry or
group generations. Playlist/session owners continue to own playback tracks.

Mix learning consumes these broad descriptors. It never creates a track-vector
centroid, including by finding today's tracks with matching genres. A historical
genre area can map to the common ancestor of indistinguishable current leaves;
it cannot supply child-specific wins. Group formation and splitting continue to
use current musical evidence. Raw genre facets supply bounded choice influence;
the theme remains part of evidence identity and correlation limits.

Old menus lacking frozen rendered genre values are explicitly marked as a
legacy reconstruction. Captured track genre strings can be retained as known
partial labels, but missing artist-fallback labels are not reconstructed from
today's catalog and incomplete sets do not support preference learning.

Preparation is explicit and restartable. It publishes compact projections before
releasing their historical group roots. Old immutable JSON stays readable during
preparation, and its final removal belongs to #626. Current grouping, required
operational predecessors, active builds, readers and actual outputs keep their
own roots. Independent Album and Radio evidence keeps its own model inputs;
unrelated group fields do not become roots of those formats.

The additive schema is regular change 11. Its complete DDL is in
`core/mix_choices.py`: immutable raw labels, equal-membership genre sets,
themes, descriptors, per-menu option references and projection progress. The
existing canonical genre dictionary is taxonomy-owned; these captured labels
must survive changes to that taxonomy. Current and installed prepared learning
publications reference descriptors through small extension relations.

Run explicit preparation with:

```sh
uv run python -m yt_playlist.core.preparation.mix_choices /path/to/copy.db --batches 100
```

Each invocation resumes its last committed prefix. Menus publish projections
first; learning publications follow; current Home displays are replaced through
their normal guarded publication API; obsolete choice roots are then released
128 version references at a time. No bulk step runs during application startup.
Failed event publication leaves an unacknowledged SQL projection that ordinary
maintenance reconciles under the same publication admission lock.

Backups include all six explicit listening-evidence file kinds, with bounded
manifest pages, hashes and owner checks. SQL snapshots exclude newly published
menus whose descriptor ownership postdates the snapshot. File retirement waits
for readers and backups. Recovery restores verified immutable files before the
disposable database is used.

The actual-library reproducer is `experiments/retained_copy/mix_choices.py`.
It creates and removes its own temporary workspace, preserves the source
checksum, compares the complete current head and decision receipts, checks late
and repeated writes, and reports SQL/index bytes separately from group packs.
Its larger collection batches drain the existing background collector only in
that disposable process; their duration is not production foreground latency.
Old shared evidence JSON remains a separate #626 cleanup, and SQLite free pages
are reclaimed by #658 rather than this preparation.

Actual-library result (2026-11-01, `backups/688-mix-choices-report.json`):

| Retained group/choice data | Bytes |
| --- | ---: |
| SQL and indexes before preparation | 292,913,152 |
| SQL and indexes after collection | 9,322,496 |
| Repacked group files (two packs) | 440,970 |
| SQL after 40 additional controlled menus | 9,338,880 |

All 437 current groups and 25,225 memberships compare equal as a complete
head before and after collection. The 117 historical mix menus retain their
decision envelopes; all 1,539 response events across formats remain unchanged.
183 descriptors share 163 raw-genre sets; 13 legacy options have incomplete
genre provenance. Twenty repeated menus add no descriptors; twenty menus with
changed labels/genres add 14. A late response to the original cohort succeeds
without creating a group pin. Foreign keys pass and the source SHA-256 remains
`3e2b6a5e31d0d6bd57630b87fc34d367518e9ee9895ff6b88ad9661dc41dfa51`.

Preparation took 50.69 seconds. Draining collection with the experiment's
8,192-row batches took 222.66 seconds. Twenty subsequent group-head reads took
108.5–132.1 ms (p95 123.4 ms); these are isolated reader timings, not an
interactive whole-application benchmark. The experiment removes its temporary
database and files on exit. This does not claim a physically compacted whole
database or removal of old shared evidence JSON.
