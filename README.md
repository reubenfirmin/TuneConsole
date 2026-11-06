# TuneConsole

**A better algorithm.**

Works with your existing YouTube Music account. TuneConsole learns from your library
and listening history to bring you new discoveries, neglected records, and old
favorites. Shape your next mix, follow connections between songs, or switch between
four radio streams as you listen.

Free app and extension. Recommendations run on your computer, and your YouTube Music
sign-in stays in your browser. Bring your Spotify playlists and saved albums along
with the included import.

[**Get TuneConsole**](https://tuneconsole.com/#install) ·
[Explore tuneconsole.com](https://tuneconsole.com) ·
[Privacy policy](https://tuneconsole.com/privacy)

[![Personalized mix tapes: music close to your taste, overlooked records, new discoveries, and old favorites.](https://tuneconsole.com/shots/home.webp)](https://tuneconsole.com)

<table>
  <tr>
    <td width="50%" valign="top">
      <a href="https://tuneconsole.com/shots/radio.webp"><img src="https://tuneconsole.com/shots/radio.webp" alt="TuneConsole Radio with a playing song and four streams to choose from." width="100%"></a>
      <strong>Radio that follows your taste.</strong> Four streams to explore and switch between.
    </td>
    <td width="50%" valign="top">
      <a href="https://tuneconsole.com/shots/clusters.webp"><img src="https://tuneconsole.com/shots/clusters.webp" alt="An interactive map connecting songs from M83, Shpongle, Pink Floyd, and other artists." width="100%"></a>
      <strong>Find a new path through your music.</strong> Explore connections and turn your discoveries into playlists.
    </td>
  </tr>
</table>

## Features

- **Cross-identity consolidation**: sign in to several YouTube brand accounts and merge or move
  playlists into one master library.
- **Dedupe, merge & prune**: find duplicate and overlapping playlists, merge them, and delete
  empties. Every destructive action is undoable.
- **Omnisearch**: instant search across playlists, artists, albums, and tracks in your whole library.
- **Library browsing**: dedicated Artists, Albums, and Charts views.
- **Song actions**: create a playlist from a song or add it to an existing playlist from the shared ⋯ menu.
- **Clusters**: a fun visual approach to building playlists.
- **Road Trip**: build a playlist for a road trip that combines your and passenger's tastes.
- **Recommendations & discovery**: surfaces new artists, rediscoveries, and a personalized
  "for you" feed driven by the taste model.
- **Metadata enrichment**: uses Last.fm, Deezer, Discogs, MusicBrainz and AcousticBrainz.
  Edit recognized genre tags under **Enrichment / Advanced**.
  MusicBrainz, Last.fm and Discogs share one vocabulary, seeded from MusicBrainz plus curated
  names and aliases. Expand or search the genre families there; equivalent names share an entry,
  while distinct subgenres retain their identity. Vocabulary upgrades preserve custom additions
  and disabled genres. Changes apply to future matching; existing song tags stay as saved.
- **Album descriptions**: Long play can optionally use your DeepSeek Flash API key in
  Settings / Enrichment. Descriptions are saved locally and reused; generation defaults to
  at most 20 requests per UTC day, with a configurable limit and pause switch. Only artist,
  album title and year are sent. `DEEPSEEK_API_KEY` or `deepseek_api_key` in config.toml
  also work; the environment takes precedence over a saved key.

## Quickstart

1. **Start the app:**
   ```bash
   uv sync                 # requires Python 3.12+; uv can download a compatible interpreter
   uv run yt-playlist        # serves http://127.0.0.1:8765
   ```
2. **Install the browser extension** (Chrome/Chromium/Edge): go to `chrome://extensions`, turn on
   **Developer mode**, click **Load unpacked**, and select the `extension/` directory. It connects
   to the app automatically, there is nothing to paste (see `extension/README.md`).
3. **Open `https://music.youtube.com` signed in**. The app pairs with the extension and starts syncing your library in the background.

HTTP access logs are disabled by default. To include request logs in `app.log` and terminal
output, start with `YT_PLAYLIST_ACCESS_LOG=1 uv run yt-playlist` (also works with `--reload`).
Application messages and server error tracebacks remain enabled regardless of this setting.

The new Charts Home journal and Listening analytics views are enabled for feedback.
Start with `YT_PLAYLIST_LISTENING_CHARTS=0 uv run yt-playlist` to hide both views and
stop their background preparation. Set the variable to `1` to enable them explicitly.
The existing Charts rankings remain available in either mode.
Packaged apps and containers use the same environment variable.

Audio collection runs independently of developer vibe ratings. With a compatible
extension model installed, ordinary app pages collect three ten-second excerpts
during natural playback, near 30 seconds, halfway and two-thirds through a song.
Keep a TuneConsole page open. Playback never seeks, and one app tab owns capture.
Audio and inference stay in YouTube Music; only bounded measurements and frozen
embeddings reach the app. Compatible measurements use the bundled Emotify vibe
checkpoint and seed bounded artist/genre predictions for unmeasured recordings.
Missing measurements retain the provider-metadata fallback. The existing six
vibes and human annotations retain their meanings.

The backend uses bundled individual models for Cruise, Release, Relax, Uplift and
Energize; Focus uses the validated shared checkpoint. Existing compatible
captures are rescored in background batches. Set
`YT_PLAYLIST_AUDIO_VIBE_CRUISE_MODE=off`, `YT_PLAYLIST_AUDIO_VIBE_RELEASE_MODE=off`,
`YT_PLAYLIST_AUDIO_VIBE_RELAX_MODE=off`, `YT_PLAYLIST_AUDIO_VIBE_UPLIFT_MODE=off`
or `YT_PLAYLIST_AUDIO_VIBE_ENERGIZE_MODE=off`
in `.env.local` to restore that vibe's
shared scores. Each defaults to `active`; no extension update is needed for
these backend scorers. Shared classification and radio intensity retain their
existing score scales.

Public extension archives currently exclude the optional research model assets.
Those builds remain idle for automatic collection. Local model preparation is
documented in `experiments/audio_lab/README.md`; reload the extension afterward.
This checkpoint does not establish public model redistribution or population
accuracy. See `experiments/audio_lab/MODEL_CHECKPOINT.md` for results and limits.

The developer rating banner defaults off. To enable it, put
`YT_PLAYLIST_AUDIO_VIBES=1` in the checkout's ignored `.env.local`, or set that
environment variable explicitly. An explicit environment value takes precedence;
`0` hides ratings without disabling collection. The banner accepts multiple
existing vibe choices and preserves the prediction and sample evidence at each
explicit edit. Regular users are never asked to label vibes. The dedicated Audio
analysis preview retains its immediate manual/automatic single-excerpt behavior.

### Docker / Podman

With Docker or Podman installed on the same computer as your browser:

```bash
docker run -d --name tuneconsole \
  -p 127.0.0.1:8765:8765 \
  -v tuneconsole-data:/data \
  ghcr.io/reubenfirmin/tuneconsole:latest
```

For Podman, replace `docker` with `podman`. Open <http://127.0.0.1:8765>, install the
browser extension, and keep a signed-in YouTube Music tab open. The named volume preserves
your library, configuration, logs, and backups when you replace the container.

Images support Linux x86-64 and ARM64. See [container packaging](packaging/README.md#docker--podman)
for updates, local builds, and verification.

### Playlist creation dates

**New playlists** duplicates non-generated playlists created during the past seven days.
It reads `playlists.created_at`, never `first_seen`, `last_seen`, or the last edit time.
TuneConsole records its own creations immediately. The browser connection does not supply
creation dates for playlists created elsewhere; unknown dates stay out of New.
Existing saved dates are preserved during library sync. YouTube Music authentication stays
in the browser, with no separate Google OAuth connection.


## Tests

Run focused checks with the normal command, for example:

```bash
uv run pytest tests/rec/test_ranking_playback_publication.py --no-cov
```

Pytest stores disposable fixtures in a unique directory under this checkout's
`build/pytest`, using the checkout filesystem instead of shared `/tmp` capacity.
Concurrent focused runs use separate directories and do not acquire the global
suite lock. Each function's fixtures are removed after reporting, and session
fixtures are removed when the session finishes, including failed runs.
Reports and screenshots to retain must go outside the disposable directory.
The application's disk-space reserve still applies to copied test databases.

Use `--fixture-root=PATH` to choose another parent; pytest creates a unique base
for each session there too. An explicit `--basetemp=PATH` takes precedence and
uses that exact directory, so give concurrent runs different paths. Collection
without fixture use does not create a fixture base.

After a killed run, preview its abandoned default base with
`./scripts/test-artifacts-cleanup.py build/pytest/run-PID-UUID`, then repeat with
`--delete` to remove it. The command refuses a live owner or any process still
using the directory; it preserves other sessions, reports, and shared lock files.

## Architecture

### Database upgrades and catalog identity

Startup checks the migration history and required schema structure before binding
repositories. Installed schemas from version 79 onward are admitted after those
checks. Installed owners remain writable without waiting for a full-library audit
or bulk conversion. Startup never schedules the historical conversion chain.
Fresh databases initialize directly. Versions before 79 require the explicit
historical upgrade route before they can use this compatibility interval.

The historical layout registry is `src/yt_playlist/core/migrations/registry.py`.
Keep its applied version history intact. New bounded additive changes belong in
`core/migrations/regular.py`, with their own `schema_changes` ledger. Its first
change installs playback receipts independently of metadata conversion: a failed
v80 cannot roll back that installation or cause an unfinished v80 stamp. Both
runners own their transactions, version stamps and rollback; callbacks must never
commit, use `executescript`, or call repository write methods.

`uv run python -m yt_playlist.core.migrations upgrade-schema /absolute/path/to/library.db`
applies only the independent regular changes. It validates the installed schema
structure, never invokes the historical bulk-conversion chain and leaves its
`schema_migrations` history intact. Each new regular change must remain compatible
with supported installed formats while their data preparation is pending.

Each regular change and its own version stamp commit atomically.
SQLite's transaction journal handles rollback and crash recovery; the framework does
not create full-database snapshots or maintain a backup collection. If a migration
fails, its changes roll back while previously completed versions remain committed,
and bounded schema installation can retry on the next launch or through the
maintenance command. A failed one-time preparation keeps earlier checkpoints
and needs an explicit retry; its installed owners remain writable. Missing or incompatible
required schema objects still prevent startup. Normal advancement checks schema
structure and retains the safety checks of each individual change; it does not
repeat a global foreign-key/data audit between supported installed versions.

Run a full-library integrity audit explicitly with
`uv run python -m yt_playlist.core.migrations audit /absolute/path/to/library.db`.
It opens the database read-only and checks one committed snapshot. Failure exits
nonzero without changing data or migration history. This can take time on a large
library and uses a separate reader; it does not pause application writes or retry
a conversion. A passing audit describes that snapshot, not subsequent writes.

The preparation work is tracked by #621 and
#622 and documented in [the schema inventory](SCHEMA_OWNERSHIP.md#startup-migration-and-conversion-boundary).
Bulk conversion must become resumable one-time preparation, with compatible
reads and writes until a future cleanup. It must not block regular migrations.

The first explicit preparation job covers listening subjects on genuine installed
layouts 79–99. It builds additive staging tables, retaining both serving owners:

```sh
uv run python -m yt_playlist.core.preparation.listening_subjects /absolute/path/to/library.db --batches 32 --batch-size 32
uv run python -m yt_playlist.core.preparation.listening_subjects /absolute/path/to/library.db --status
```

Each invocation commits at most the requested number of batches, with at most
32 source rows and bounded source bytes per transaction. Closing or
interrupting it retains earlier checkpoints. After repairing a reported input,
pass `--retry` to resume a failed job explicitly. A busy writer makes the command
fail promptly; it does not start an automatic retry loop. Neither command creates
a missing database, and `--status` opens it read-only.

The old tables remain authoritative for writes and for reads before selection. Installed triggers
journal changed keys, including deletions and reused IDs behind a checkpoint.
Preparation reconciles those keys before recording a prepared revision; a later
write invalidates that revision until another explicit run catches up. Recording
key changes restart a bounded scan without doing that scan inside the live write.
Staging retains exact source snapshots and does not infer an old membership from
today's playlist. It has no foreign keys that could block ordinary source resets.

The separate subject publisher retains typed events, observations, source
contributions, event and account calendars, capture receipts, canonical credit
attribution, and account/recording and account/artist summaries:

```sh
uv run python -m yt_playlist.core.preparation.subject_publication /absolute/path/to/library.db --batches 32 --batch-size 32
uv run python -m yt_playlist.core.preparation.subject_publication /absolute/path/to/library.db --publish-stage STAGE_ID
```

Repeat bounded staging invocations until `complete` is true. Selection checks
exact source snapshots, typed rows, derived summaries and complete row coverage
outside a writer transaction, then rechecks the captured revisions in a short
selection transaction. Readers switch the whole listening projection together.
Existing connections discover the additive views; pinned readers retain their
original snapshot. A capture, calendar, account, catalog or canonical credit
change withdraws selection immediately. Live writes keep using the installed
tables; reconcile preparation and stage again before selecting their new output.
Interrupted staging retains committed batches. A publication error exits without
automatic retry or changing a completed generation. Retained stages and originals
remain in place for future cleanup. Native v100+ subject owners take precedence.

Neither preparation nor publication advances `schema_migrations` or retires old
storage. Extraction of the remaining conversions is still tracked in #622. The historical chain is
retained for explicit schema-history replay and does not run at startup.
Future cleanup is tracked in #626, alongside #620's read-adapter removal gates.
The audit/regular-ledger separation is implemented. Extraction of the remaining
bulk conversions and compatible older-format writes is still in progress; the
remaining publications do not yet cover the complete extraction inventory.

Additional explicit jobs prepare v79 metadata evidence and the bounded work,
offer and album-request documents that still use proposal storage:

```sh
uv run python -m yt_playlist.core.preparation.metadata_evidence /absolute/path/to/library.db --batches 32
uv run python -m yt_playlist.core.preparation.recommendation_state work /absolute/path/to/library.db --batches 8
uv run python -m yt_playlist.core.preparation.recommendation_state home-offers /absolute/path/to/library.db --batches 8
uv run python -m yt_playlist.core.preparation.recommendation_state album-requests /absolute/path/to/library.db --batches 8
```

They share the same explicit `--status`, `--retry`, `--batch-size` and `--batches`
controls. Metadata preparation supports genuine v79; the work, offer and
request jobs support layouts 79–81, 79–82 and 79–83 respectively. Their staging
tables do not activate runtime repositories. Composite source keys survive
row-ID changes, and changes to referenced genre/catalog or album identities
invalidate the captured preparation proof. Metadata retains exact original rows
alongside typed value classification, including unknown subject/scope and native
provenance. Operational records retain their originals for the remaining
destination-link preparation. Conflict lists use the separate bounded worker
below. The request job prepares demand only.
Metadata staging retains every observed source revision and expands supported
provider attempt/state fields separately, including null/blank catalog values and
exact noncatalog inputs. It preserves unknown historical input entries. Provider
field maps have a 256 KiB byte budget; metadata batches admit at most 16 source
rows before expanding their finite fields. Invalid maps require repair and an
explicit retry. This does not activate final delivery/observation owners.
Work originals have a 16 KiB byte budget; offer and album-request originals have
a 256 KiB budget each, including whitespace. An oversized document fails
explicitly and is never truncated into a different accepted value.

Metadata conflict evidence on v79 uses an ordered candidate job:

```sh
uv run python -m yt_playlist.core.preparation.metadata_conflicts /absolute/path/to/library.db --batches 16 --batch-size 128
```

Each batch stages at most the requested individual candidates, including repeated
offers and invalid candidates with their original positions. Exact original rows
are retained under `DATABASE.preparation/metadata-conflicts`, with an 8 MiB source
byte budget. Decoding and durable file writes occur outside SQLite transactions.
Saved resolutions remain independent decisions; equal values never establish
which candidate was selected. Blank and missing resolutions stay distinct, and
malformed bodies remain archived with an explicit disposition. Source and catalog
edits invalidate the proof. Status and explicit retry use the same controls.
The installed conflicts remain authoritative until an explicit verified selection:

```sh
uv run python -m yt_playlist.core.preparation.conflict_publication /absolute/path/to/library.db --batches 16 --batch-size 16
uv run python -m yt_playlist.core.preparation.conflict_publication /absolute/path/to/library.db --publish-stage STAGE_ID
```

The publication maps at most 16 conflict generations per transaction. Verification
checks original files, complete source coverage, typed candidates, saved decisions
and subject references outside the final writer; selection changes only one head.
Review and count readers use the selected relation. Continuing enrichment writes
retain the installed source and withdraw that selection until reconciliation.
Candidate order, duplicates, blank/null values, malformed bodies, unsupported
fields and unknown historical subjects survive without inferred provider links.
Native v80 evidence takes precedence after an explicit historical upgrade.
Use `--repair-conflict CONFLICT_ID` to recreate an absent original only when its
installed source still has the captured exact digest; otherwise reconcile first.
The additive compatibility capture makes an older prepared job replay its source
under a new revision. Final metadata delivery/observation and operational links
are separate remaining preparation work.

Prepared metadata also has a separately verified read publication:

```sh
uv run python -m yt_playlist.core.preparation.metadata_publication /absolute/path/to/library.db --batches 32 --batch-size 16
uv run python -m yt_playlist.core.preparation.metadata_publication /absolute/path/to/library.db --publish-stage STAGE_ID
```

It stages at most 16 source rows per commit and verifies exact originals, every
typed field, operational field order, current references and complete coverage
outside the final writer reservation. Provenance, native findings, attempts and
retry-state reads use the selected immutable relations. Compatible writers keep
their installed owners and withdraw selection atomically until reconciliation.
Null, blank and repeated operational values remain distinct. All text is clipped
before transfer from SQLite; an original above the 1 MiB source budget or a
provider map above 256 KiB fails explicitly without accepting truncated data.
Native v80 owners take precedence. Retained delivery files and explicit typed
observation links have their own subsequent publication:

```sh
uv run python -m yt_playlist.core.preparation.metadata_destinations /absolute/path/to/library.db --stage STAGE_ID --batches 32 --batch-size 16
uv run python -m yt_playlist.core.preparation.metadata_destinations /absolute/path/to/library.db --stage STAGE_ID --status
uv run python -m yt_playlist.core.preparation.metadata_destinations /absolute/path/to/library.db --stage STAGE_ID --publish
```

Use the completed metadata read stage ID. Delivery packing and file verification
run outside SQL transactions; each destination batch commits at most 16 captures.
Distinct original records keep distinct delivery identities. Choices, hints,
identifiers and operational fields link to their supplying delivery; supported
catalog values have finite typed observations. Historical source labels remain
explicitly unresolved attribution with no observation link. Equal values never
establish lineage. Typed catalog base values keep their existing owners, as
agreed in #494.

Native finding pages and musical-source inputs read supplying delivery files.
Before selection, each source projection must exactly match its retained native
evidence. Source-cache writes also withdraw selection and require a fresh read
stage. All installed source owners and old published generations remain retained.
An interrupted batch or failed verification records a separate recovery error;
regular changes remain independent. Repair the cause, then pass `--retry` to the
batch command. For a missing delivery pack, `--repair-delivery DELIVERY_ID`
reconstructs only its exact bounded pack from immutable original records;
different bytes or ranges fail rather than replacing captured history.

Home demand and held listening windows on layouts 79–86 use another job:

```sh
uv run python -m yt_playlist.core.preparation.recommendation_demand /absolute/path/to/library.db --batches 16 --batch-size 1
```

It retains the five exact originals and stages their ordered exclusions, genre
choices, hold flags and opportunity identities. Repeated choices remain ordered;
missing genre/album identities remain unresolved. Source and reference edits
invalidate the proof. The existing proposals continue serving reads and writes.
It accepts the same status/retry controls, with a 256 KiB original byte budget.
When preparation reports `prepared`, stage and select the request owners:

```sh
uv run python -m yt_playlist.core.preparation.demand_publication /absolute/path/to/library.db --batches 16 --batch-size 16
uv run python -m yt_playlist.core.preparation.demand_publication /absolute/path/to/library.db --publish-stage STAGE
```

Each stage batch commits at most 16 rows. Verification compares exact originals,
all typed request members and their current identity references outside the final
writer transaction. Selection checks the source and custody revisions and changes
one head. Empty held windows retain their native absent-result semantics; empty
Home and album requests remain distinct from missing requests. Continuing writes
keep the installed proposals and withdraw selection in the same transaction.
Reconcile preparation, stage again and select the new generation after edits.
Earlier stages and source documents stay retained. A genuine v87 upgrade takes
precedence; preparation and publication do not advance historical stamps. Failed
preparation leaves regular schema changes and installed-format serving usable.

Genre preferences on layouts 79–84 use two separate jobs:

```sh
uv run python -m yt_playlist.core.preparation.genre_preferences enabled /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.genre_preferences defaults /absolute/path/to/library.db --batches 16 --batch-size 128
```

The enabled job retains exact labels, custom intent and ambiguous resolutions.
The defaults job retains the exact saved baseline, ordered labels and seed-marker
meaning under `DATABASE.preparation/genre-defaults`. It preserves the distinction
between disabled old defaults and defaults never introduced. Both accept the
same status/retry controls and leave the installed whitelist authoritative.
Neither job creates identities or publishes a runtime owner.

After both jobs reconcile, publish the preference selection separately:

```sh
uv run python -m yt_playlist.core.preparation.genre_preference_publication /absolute/path/to/library.db --batches 16 --batch-size 16
uv run python -m yt_playlist.core.preparation.genre_preference_publication /absolute/path/to/library.db --publish-stage STAGE_ID
```

An enabled label marked `requires_custom` needs explicit bounded admission using
`genre_preference_publication DATABASE --admit-custom --batch-size 16`. This
creates identities only for retained enabled choices after comparing their live
source intent. Reconcile both preparation jobs again after admission. Ambiguous
enabled labels require an explicit catalog/alias or source correction first.

Publication copies at most 16 labels per transaction, retaining repeated ordered
defaults and the exact baseline file. Independent verification checks live
choices, default and seed-marker meaning, typed values and complete coverage
before the short selection transaction. Disabled introduced defaults remain
disabled; never-introduced defaults remain absent. Compatible whitelist/settings
and reference edits withdraw selection. Readers then use the installed sources
until reconciliation and another publication; native v85 preferences take
precedence. Missing baseline files can be rebuilt from their unchanged sources
with `genre_preference_publication DATABASE --repair-baseline`; regular schema
admission does not read those files. Original evidence, generations and applied
historical stamps remain retained.

Taste axes (layouts 79–87) and recipe membership (layouts 79–88) also have
independent staging jobs:

```sh
uv run python -m yt_playlist.core.preparation.taste_axes /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.recipe_memberships /absolute/path/to/library.db --batches 16 --batch-size 128
```

Taste staging keeps exact contributions and event IDs; competing configured
weights/leans are listed in `prepared_taste_conflicts` for explicit resolution
before publication. Recipe staging batches individual ordered members and keeps
exact original lists under `DATABASE.preparation/recipe-memberships`. Repeated
canonical selections retain a `duplicate_position` witness instead of losing
data. Source and identity edits invalidate prepared revisions. These jobs accept
the same status/retry controls and leave installed runtime owners authoritative.
Reconciled taste axes have a separate bounded publication:

```sh
uv run python -m yt_playlist.core.preparation.taste_publication /absolute/path/to/library.db --batches 16 --batch-size 16
uv run python -m yt_playlist.core.preparation.taste_publication /absolute/path/to/library.db --publish-stage STAGE
```

Stages copy at most 16 contributions per commit. Verification checks every
original, current identity, typed value and complete coverage outside the final
writer reservation. Selection changes one head after checking source and custody
revisions. Theme scores add, graduation days take the maximum, hidden bars dedupe
and all graduation event IDs remain individual. Configured weights/leans never
choose between competing aliases. Distinct identities that share an API token
also require source/reference reconciliation before publication.

Continuing writers keep the installed owners and withdraw immutable selection
atomically. Canonical API keys update their current physical aliases, including
aliases added since staging. Reads then fold the current installed contributions
until preparation and publication reconcile again; separate alias contributions
cannot become duplicate graduation targets. Conflicting current configured values
or genre/family meanings remain visible under their raw keys. Earlier stages and
exact source evidence remain retained. Native v88 owners take precedence after a
genuine upgrade; historical stamps do not advance during preparation/publication.

Reconciled recipe membership has its own bounded map and verified selection:

```sh
uv run python -m yt_playlist.core.preparation.recipe_publication /absolute/path/to/library.db --batches 16 --batch-size 16
uv run python -m yt_playlist.core.preparation.recipe_publication /absolute/path/to/library.db --publish-stage STAGE
```

Verification compares every original list and typed member against the installed
source and current references outside the final writer reservation. Duplicate
resolved identities require explicit source correction; unresolved repeated labels
retain their order. Selected reads use canonical labels and exact unresolved
labels, matching native v89 membership. Ordinary recipe writers retain the old
columns and withdraw selection atomically, as do reference edits. Native v89
owners take precedence. Earlier maps, members and files remain retained. Recover
a missing file for an unchanged recipe with `recipe_publication DATABASE
--repair-recipe RECIPE_ID`; admission does not read those files.

The explicit v88 upgrade detaches obsolete taste preparation dependency
triggers before replacing the six legacy axis owners. Regular change 4 also
repairs an already-upgraded installation carrying those exact programs. Staged
originals and historical migration stamps remain retained.

Listening display files on layouts 79–89 have separate file and link jobs:

```sh
uv run python -m yt_playlist.core.preparation.listening_outputs files /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.listening_outputs links /absolute/path/to/library.db --batches 16 --batch-size 128
```

Run files to a prepared revision before links. File preparation retains exact
documents under `DATABASE.preparation/listening-outputs` and batches individual
manifest references. Link preparation checks each selected chunk and its recorded
byte size; a missing chunk fails durably and requires repair and explicit retry.
Source edits invalidate both proofs. Limits are 1 MiB per document, 4,096 chunks
and 128 MiB of encoded files. Existing proposals remain authoritative during
preparation. Once files are reconciled, stage a bounded immutable runtime map
and publish its reported stage ID separately:

```sh
uv run python -m yt_playlist.core.preparation.listening_publication /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.listening_publication /absolute/path/to/library.db --publish-stage STAGE
```

Publication independently verifies exact source/file bytes, observed times,
ordered manifest references, whole-source coverage and each selected chunk's
original byte count outside writer reservations. Its immutable map captures
manifest and chunk generations together, including retained unselected chunks.
The separate staging link job can reconcile newer heads without changing those
captured relationships. A bounded final transaction checks source and custody
revisions again and selects the map. Preopened proposal and analytics readers
discover it immediately; physical v90 owners take precedence after upgrade.

Continuing analytics writers retain the installed proposal format and atomically
withdraw selection. Reconcile and publish their next revision explicitly. Prior
maps and captured file rows remain immutable; mutable preparation counters and
link proofs keep working. Missing or corrupt selected files serve the native
cache-miss result. Restore a missing file from an unchanged captured source with:

```sh
uv run python -m yt_playlist.core.preparation.listening_publication /absolute/path/to/library.db --repair-source listening_analytics
```

The same command accepts `listening_edition` or a `listening_chunk:KEY` surface.
Recovery restores content-addressed files without rewriting SQL custody;
corrupt existing files require operator quarantine before recovery. Changed
sources require reconciliation. Interrupted batches resume; failed preparation
requires explicit `--retry`. Regular schema admission neither verifies these
files nor waits for preparation or publication.

Home displays on layouts 79–90 have corresponding file and retention-root jobs:

```sh
uv run python -m yt_playlist.core.preparation.home_outputs files /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.home_outputs roots /absolute/path/to/library.db --batches 16 --batch-size 8
```

File preparation retains exact display text under
`DATABASE.preparation/home-outputs`, with an 8 MiB source budget. Each batch
stages individual file parts or explicit policy references. Unknown policies
remain unresolved. Run files to a prepared revision before roots; the root job
archives the six installed retention owners for the ten supported Home surfaces,
including their existing unresolved contexts and aliases. It admits at most eight
roots and 2 MiB of source text per batch. Existing retention records remain in
place; preparation neither invents evidence links nor releases source pins.
Source, policy and retention edits invalidate the corresponding proofs. Both
jobs use the same status and retry controls. After both jobs report `prepared`,
capture and publish their immutable map:

```sh
uv run python -m yt_playlist.core.preparation.home_publication /absolute/path/to/library.db --batches 16 --batch-size 8
uv run python -m yt_playlist.core.preparation.home_publication /absolute/path/to/library.db --publish-stage STAGE_ID
```

The first command reports a stage ID and `complete`; run it again until complete.
Publication verifies exact files, display metadata, policies and all six retention
owners outside the writer reservation. A bounded transaction selects the map only
if its inputs still match. FK-backed copies retain the captured evidence when a
later display releases the original pins. Compatible writes withdraw selection
and keep serving the installed proposals while the jobs reconcile. Native v91
owners take precedence after historical upgrade. Missing selected files are cache
misses; `--repair-surface SURFACE` recreates unchanged-source files without changing
SQL custody. None of these commands stamps historical conversion complete or
deletes retained sources; pin retirement belongs to gated future cleanup #626.

Diagnostic reports on layouts 79–92 use a separate file job:

```sh
uv run python -m yt_playlist.core.preparation.recommendation_reports /absolute/path/to/library.db --batches 8 --batch-size 1
```

It retains exact report text in verified file parts under
`DATABASE.preparation/recommendation-reports` and stages the queried refresh,
identity and Home-format usage fields. Each batch admits at most four fixed
report surfaces, each with at most 64 parts and nine usage fields; file decoding
and durable writes occur outside the database transaction. Replacement and key
renames invalidate the proof while prior generations remain retained. Status
and explicit retry use the same controls. This stages diagnostic reports only;
choice learning has its own conversion boundary.

Publish a reconciled report revision explicitly:

```sh
uv run python -m yt_playlist.core.preparation.report_publication /absolute/path/to/library.db --revision REVISION
```

The command verifies exact source bytes, files and typed fields outside a database
transaction, then rechecks the revision and selects at most four report heads in
one bounded transaction. Existing and new readers use the selected files and
queried fields. A source write atomically invalidates that selection, so reads
immediately use the updated installed document until preparation and publication
select its next revision. This preserves continuing old writers. Prior published
generations remain immutable and retained. Report usage and refresh fields remain
readable even if an optional diagnostic file is missing. No historical migration
is stamped and no source document is removed.

Saved choice-learning inputs on layouts 79–92 use a separate relation job:

```sh
uv run python -m yt_playlist.core.preparation.choice_learning /absolute/path/to/library.db --batches 16 --batch-size 128
```

It retains exact documents under `DATABASE.preparation/choice-learning` and
checkpoints individual estimates, opportunity IDs, feature coordinates, recording
keys and album listening-day records. A long nested list cannot enter one writer
batch. Staging reuses the frozen numerical contracts while retaining unknown
mode, album, recording and group references explicitly. It observes canonical
reference IDs without adding retention pins that could block source resets.
Source and reference edits invalidate the proof; older generations remain
retained. The source byte budget is 32 MiB, including whitespace. File work runs
outside SQLite transactions, and invalid input or disk failure preserves earlier
commits and requires explicit retry. The installed proposal remains authoritative
until the separate verified publication below.

Select the prepared learning relations separately, after preparation completes:

```sh
uv run python -m yt_playlist.core.preparation.choice_publication /absolute/path/to/library.db --batches 16 --batch-size 16
uv run python -m yt_playlist.core.preparation.choice_publication /absolute/path/to/library.db --publish-stage STAGE_ID
```

Repeat the first command until `complete` is 1. It first retains the immutable
document map, then checkpoints at most sixteen exact group reference owners per
transaction. Missing historical aliases stay explicitly unresolved; known
generations and versions receive real retention roots before selection. The
second command verifies exact originals, every typed member, current reference
resolution and complete group custody outside the writer reservation. Only the
final head selection takes a writer transaction. Existing and reopened readers
then use the typed relations. A source or reference edit withdraws selection
atomically; ordinary writers keep updating the retained proposals until another
explicit reconciliation and publication. Prior maps, values and roots remain
retained for future cleanup. Native v93 owners take precedence after a genuine
frozen upgrade. To recover a missing original file whose source is unchanged,
use `--repair-source choice_interests` (or the affected learning surface); changed
sources require preparation again.

Album-note output files on installed layouts 79–83 have a separate explicit job:

```sh
uv run python -m yt_playlist.core.preparation.album_notes /absolute/path/to/library.db --batches 16 --batch-size 8
uv run python -m yt_playlist.core.preparation.album_notes /absolute/path/to/library.db --status
```

It retains exact proposal bytes and ordered note generations under
`DATABASE.preparation/album-notes`. Decoding, hashing and durable file writes
happen outside SQLite transactions. Each batch admits at most the requested
notes after checking the source revision again. Source replacements, deletions,
key renames and album-admission changes invalidate the proof. Unadmitted album
IDs remain unresolved. Disk failure preserves committed batches and requires
`--retry`; older files stay retained outside serving collectors. The existing
proposal remains authoritative during preparation. The limits are 128 notes,
1 MiB per encoded note and 128 MiB per original proposal; invalid input fails
without blocking normal schema changes.

Select its verified prepared revision separately:

```sh
uv run python -m yt_playlist.core.preparation.album_note_publication /absolute/path/to/library.db --revision REVISION
```

Verification compares exact original bytes, note order, file bytes, metadata and
observed provider references outside writer reservations. The final transaction
selects one generation after checking the source and custody revisions again.
Preopened readers use the selected files; source/provider changes atomically
withdraw selection and resume installed proposal reads. Existing note writers
continue using the proposal, and preparation reconciles their changes. Empty or
absent sources serve the native empty result while retaining their distinct
source-presence evidence. Paid descriptions and request-completion fences keep
their own owners.

Interrupted preparation resumes committed batches; interrupted publication
rolls back. Captured generations and files stay retained outside serving
collectors. Physical v84 owners take precedence after historical upgrade. An
unchanged source can restore missing original/output files without rewriting SQL
custody:

```sh
uv run python -m yt_playlist.core.preparation.album_note_publication /absolute/path/to/library.db --repair-files
```

Re-publish after recovery. Changed sources require reconciliation, and failed
preparation requires explicit `--retry`. Missing files or failed preparation do
not hold up regular schema changes.

Saved Radio checkpoints on genuine layouts 79–93 have a separate queue job:

```sh
uv run python -m yt_playlist.core.preparation.radio_session /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.radio_session /absolute/path/to/library.db --status
```

The exact settings document remains authoritative. The job retains its original
UTF-8 bytes under `DATABASE.preparation/radio`, outside serving-file collectors.
Decoding, hashing and durable file publication happen without a SQLite writer
reservation. Each committed batch resolves at most the requested queue items,
preserving repeated uploads, order, pending receipts and original episode/account
identity. Source and catalog edits invalidate the proof; revision checks reject
stale work published concurrently. Missing episode IDs and unadmitted uploads
remain explicitly unresolved. No provider operations, invented identities,
catalog admissions or runtime owner switch occur. Interrupted generations and
their files remain retained for #626's separately gated cleanup. The byte budget
is 16 MiB per original checkpoint; oversized or invalid input fails explicitly.

After that job reports `prepared`, select its exact input revision explicitly:

```sh
uv run python -m yt_playlist.core.preparation.radio_publication /absolute/path/to/library.db --revision REVISION
```

File hashing, decoding and full queue/reference verification run before the final
writer reservation. A durable custody counter also rejects queue or manifest
changes during verification; publication only selects the verified generation.
Existing readers discover the file immediately. Ordinary checkpoint saves keep
the installed settings format and atomically withdraw selection, so compatible
writers continue without a new session ID or revision protocol. Catalog/account
changes withdraw it too. Earlier published files and queue rows are immutable;
native Radio owners take precedence when already installed. A missing retained
file requires an explicit `radio_session ... --retry` to recreate it from the
unchanged source. A corrupt existing file must first be quarantined by the
operator; retry does not overwrite it. No startup path runs preparation or file
verification automatically.

Retained album feedback and expired-account flags on layouts 79–93 prepare
independently:

```sh
uv run python -m yt_playlist.core.preparation.collection_feedback album-feedback /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.collection_feedback account-auth /absolute/path/to/library.db --batches 16 --batch-size 128
```

Each job retains exact original text under
`DATABASE.preparation/collection-feedback` and commits individual map members.
Album responses retain their original judgments, times, opportunity/action inputs and unknown
author; today's active account is never assigned to them. Provider aliases retain
separate judgments even when they resolve to the same album. Unknown albums stay
unresolved without catalog admission. Authentication flags retain exact labels
and account IDs; missing accounts stay explicit. Each map has a 16 MiB byte
budget and at most 16,384 members. Source/reference edits invalidate its proof;
renames withdraw only that job's current head. A failed job leaves the other job
and regular schema changes usable. Both use the same status and explicit retry
controls. Select each verified owner using its reported `prepared_revision`:

```sh
uv run python -m yt_playlist.core.preparation.collection_feedback_publication album-feedback /absolute/path/to/library.db --revision REVISION
uv run python -m yt_playlist.core.preparation.collection_feedback_publication account-auth /absolute/path/to/library.db --revision REVISION
```

Verification checks exact original files and every ordered typed member before
the writer reservation. Publication selects only a generation; a durable custody
counter rejects concurrent manifest or member edits. Published inputs remain
immutable, including unresolved identities and alias judgments. Existing readers
discover selection immediately. Compatible writes keep the installed settings
format and atomically withdraw only the affected selection; native owners take
precedence. Explicit preparation retry can restore missing files from an unchanged
source without rewriting published rows. Quarantine a corrupt existing file before
retrying. Routine startup validates finite definitions without reading files or
running preparation.

Road-trip drafts on layouts 79–93 have their own job:

```sh
uv run python -m yt_playlist.core.preparation.road_trip_drafts /absolute/path/to/library.db --batches 16 --batch-size 128
```

It retains exact source text under `DATABASE.preparation/road-trip-drafts` and
checkpoints individual ordered picks and exclusions, including repeated uploads.
Known entries resolve by exact video ID; unadmitted uploads remain explicit
without catalog writes. The original author stays unknown. Recorded revisions
retain their exact value; missing, null, blank and invalid revision inputs stay
distinct without generating a replacement. Source edits, recipe deletion and
catalog changes invalidate the proof. File work happens outside SQL transactions;
failed work preserves earlier commits and requires explicit retry. The budgets
are 16 MiB and 32,768 total picks/exclusions per draft. Installed draft reads and
revision-checked writes remain authoritative until explicit publication:

```sh
uv run python -m yt_playlist.core.preparation.draft_publication /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.draft_publication /absolute/path/to/library.db --publish-stage STAGE
```

Use the completed stage reported by the first command. Each mapping batch retains
at most 128 recipe/generation links. Full original/typed-selection verification
runs outside the final writer reservation; cutover checks finite revisions and
selects the retained map. Point reads and latest-draft reopening use that map,
including preopened readers, while native v94 drafts take precedence. Continuing
edits withdraw selection and remain usable through the installed source. Legacy
conditional replacement reserves the writer before checking the revision and
rolls back a failed save. No author, revision or missing video is invented.

For a missing retained file on an unchanged current draft:

```sh
uv run python -m yt_playlist.core.preparation.draft_publication /absolute/path/to/library.db --repair-recipe RECIPE_ID
```

Recovery rewrites no captured SQL rows. Failed or incomplete work leaves regular
schema changes usable. Earlier maps and sources remain retained.

Playback attribution and pending album queues on layouts 79–93 prepare
independently:

```sh
uv run python -m yt_playlist.core.preparation.playback_context bindings /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.playback_context pending-album /absolute/path/to/library.db --batches 16 --batch-size 128
```

Both retain exact original text under `DATABASE.preparation/playback-context`.
Ordered bindings preserve repetitions and the original option conversion;
pending queues preserve repeated uploads, missing versus null accounts, a
cleared intent and absent versus empty video lists. Existing account, playlist,
album and upload references resolve by exact identity. Unknown references stay
explicit without creating catalog entries or assigning an active account.
Each source has a 16 MiB and 16,384-member budget. Source/reference changes
invalidate the proof, and writer batches reject stale preparation after file
work. Interrupted batches roll back; failed work retains earlier commits and
requires explicit retry. The jobs have independent status and failure state.
Select each verified owner using its reported `prepared_revision`:

```sh
uv run python -m yt_playlist.core.preparation.playback_publication bindings /absolute/path/to/library.db --revision REVISION
uv run python -m yt_playlist.core.preparation.playback_publication pending-album /absolute/path/to/library.db --revision REVISION
```

Exact original files, typed fields and every ordered reference are verified
before reserving the writer. Publication uses the same immutable custody and
concurrent-write fence as feedback documents. Preopened readers discover the
selected owner dynamically; source/reference writes withdraw it atomically and
native owners take precedence. Compatible playback binding writes reserve the
writer before reading, accept the historical account field and preserve that
external spelling. Pending intent writes retain the installed protocol. Explicit
retry can restore missing original files without rewriting published rows.

Radio playlist origins on layouts 79–93 have a separate job:

```sh
uv run python -m yt_playlist.core.preparation.radio_origins /absolute/path/to/library.db --batches 16 --batch-size 128
```

It retains all four original sources under `DATABASE.preparation/radio-origins`,
including null versus blank settings and repeated list positions. Exact provider
IDs resolve existing playlist scopes only when the match is unique. Missing or
ambiguous scopes stay explicit; the job never creates a scope or chooses an
active account. Each source has a 16 MiB and 16,384-member budget. Source/scope
edits invalidate preparation, file work runs outside SQL transactions, and each
bounded batch fences its progress. Status and explicit retry use the common
controls. The original settings remain authoritative for continuing writes.

After preparation finishes, select its exact input revision:

```sh
uv run python -m yt_playlist.core.preparation.radio_origin_publication /absolute/path/to/library.db --revision REVISION
```

Publication verifies the four exact originals, member order and current scope
resolution before taking the final writer reservation. Source and custody
revision checks reject stale work. Provenance reads use immutable typed members;
individual setting reads retain exact original bytes. Existing readers discover
selection dynamically. `remember` serializes its read and append with other
writers, retains the installed wire format and withdraws selection atomically.
Other source/scope edits withdraw it too. Native origin owners take precedence;
earlier selected generations and files stay retained. Explicit `radio_origins
DATABASE --retry` restores missing original files without rewriting published
members. Existing corrupt files require operator quarantine before retry.

Discovery fetch history, artist-image retries and provider refresh frontiers
on layouts 79–93 each have an independent checkpoint job:

```sh
uv run python -m yt_playlist.core.preparation.discovery_checkpoints fetch-history /absolute/path/to/library.db --batches 16 --batch-size 16
uv run python -m yt_playlist.core.preparation.discovery_checkpoints artist-images /absolute/path/to/library.db --batches 16 --batch-size 16
uv run python -m yt_playlist.core.preparation.discovery_checkpoints provider-refresh /absolute/path/to/library.db --batches 16 --batch-size 16
```

Each archives exact observed source values in its own preparation table, with
at most 16 checkpoints per committed batch, 64 KiB per original value and 4 KiB
per source key. Prefix queries use the settings key index. Existing albums,
artists and configured providers resolve by exact identity; missing owners stay
explicit without admission. Original artist-key spellings remain retained.
Source/reference edits invalidate preparation, failed work requires explicit
retry, and one job's failure leaves the others and regular DDL usable. Installed
settings continue to accept writes. After a job reports `prepared`, stage and
select its independently verified runtime projection:

```sh
uv run python -m yt_playlist.core.preparation.discovery_checkpoint_publication fetch-history /absolute/path/to/library.db --batches 32 --batch-size 16
uv run python -m yt_playlist.core.preparation.discovery_checkpoint_publication fetch-history /absolute/path/to/library.db --publish-stage STAGE_ID
```

The same commands accept `artist-images` and `provider-refresh`. Each immutable
stage retains exact original text and typed references. Verification checks
every installed source, prepared row and original in short read snapshots,
outside the final writer reservation. Source, reference and preparation edits
withdraw selection immediately and reject stale cutover. Runtime reads use the
selected typed rows; original key spellings remain distinct, including image
retry keys such as `1` and `01`. Preopened readers discover selection; pinned
readers retain their snapshot. Native owners take precedence. Earlier stages
remain retained, and routine startup validates definitions and the selected
stage reference without rerunning the full audit.

Library sync reports and their scalar stamps on layouts 79–93 have independent
jobs:

```sh
uv run python -m yt_playlist.core.preparation.library_sync result /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.library_sync stamps /absolute/path/to/library.db --batches 16 --batch-size 3
```

The report retains exact original text under `DATABASE.preparation/library-sync`
and checkpoints individual account, resource and playlist cursors. Unknown
accounts/scopes remain explicit without creating identities. Global cursors
retain their recorded ownership. Null, blank and empty reports stay distinct;
missing attempt times receive no invented timestamp. The budgets are 16 MiB and
32,768 resources per report. File work occurs outside SQL transactions and
bounded writer batches reject stale progress. Source/reference edits invalidate
the proof; earlier generations remain retained. The stamp job archives exact
source values, distinguishes null/blank/recorded inputs and commits at most
three stamps with a 64 KiB value budget. Both use the common status and explicit
retry controls. A failed report leaves stamps and regular DDL usable.

Select the verified report and stamps independently using each job's reported
`prepared_revision`:

```sh
uv run python -m yt_playlist.core.preparation.library_sync_publication result /absolute/path/to/library.db --revision REVISION
uv run python -m yt_playlist.core.preparation.library_sync_publication stamps /absolute/path/to/library.db --revision REVISION
```

Report verification checks exact files and every cursor/reference outside the
final writer reservation. Stamp verification checks all three exact originals
and typed values; publication copies at most three small typed references.
Published report inputs and stamp originals remain immutable. Source writes
atomically withdraw the affected selection, while account/scope changes withdraw
only the report. Existing readers discover selected owners dynamically and native
owners take precedence. Ordinary sync attempts keep the installed writer protocol.
Explicit report retry can restore missing original files without rewriting
published rows; quarantine corrupt files before retry. Startup checks finite
definitions and committed selection references without running preparation.

Temporary album controls and model-selection pointers on installed layouts
79–93 have separate preparation jobs:

```sh
uv run python -m yt_playlist.core.preparation.collection_controls album-controls /absolute/path/to/library.db --batches 8 --batch-size 1
uv run python -m yt_playlist.core.preparation.collection_controls model-pointers /absolute/path/to/library.db --batches 8 --batch-size 1
```

Both support `--status` and explicit `--retry`. Each source has a 64 KiB budget;
album controls contain at most 100 ordered genre selections and model pointers
contain at most four scalar run references. Exact originals preserve legacy
control values, repeated genres, leading-zero run IDs and null/blank values.
Missing or ambiguous genres and missing model runs stay explicit without
creating identities or runs. Source/reference edits invalidate preparation;
failures retain committed progress and leave the other job usable. Installed
settings continue serving reads and writes until explicit publication. These jobs
do not advance the historical migration ledger. After reconciliation, use the
job's reported `prepared_revision` to publish its verified control selection:

```sh
uv run python -m yt_playlist.core.preparation.control_publication album-controls /absolute/path/to/library.db --revision REVISION
uv run python -m yt_playlist.core.preparation.control_publication model-pointers /absolute/path/to/library.db --revision REVISION
```

Publication verifies the exact source, typed fields and resolved references in a
bounded transaction. Existing connections discover the selection dynamically.
Published generations and exact originals remain immutable. A continuing source
or reference edit invalidates selection in that writing transaction; readers use
the current installed settings until explicit reconciliation and publication.
Compatible writers keep updating the retained source. The frozen historical
upgrade can still consume that source and its native owners take precedence.
No source storage is retired by these commands.

Content, grouping and candidate-refresh checkpoints on installed layouts 79–93
have independent finite jobs:

```sh
uv run python -m yt_playlist.core.preparation.model_checkpoints content-checkpoints /absolute/path/to/library.db --batches 8 --batch-size 1
uv run python -m yt_playlist.core.preparation.model_checkpoints group-checkpoints /absolute/path/to/library.db --batches 8 --batch-size 1
uv run python -m yt_playlist.core.preparation.model_checkpoints candidate-checkpoints /absolute/path/to/library.db --batches 8 --batch-size 1
```

These use the same `--status` and explicit `--retry` controls. Each source has a
64 KiB budget and each job selects at most three fixed keys. Typed fields retain
content configuration, mode parameters, memory phases, retry/resource estimates,
group progress and exact generation tokens, and candidate fingerprints/expiry.
Missing group generations remain unresolved without creating a generation.
Null, blank and cleared objects keep exact originals. Source edits and group
generation changes invalidate preparation. Failed work leaves the other jobs and
regular DDL usable. These jobs do not fit models or process vibes. To select a
verified runtime generation, use the corresponding job's `prepared_revision`:

```sh
uv run python -m yt_playlist.core.preparation.checkpoint_publication content-checkpoints /absolute/path/to/library.db --revision REVISION
uv run python -m yt_playlist.core.preparation.checkpoint_publication group-checkpoints /absolute/path/to/library.db --revision REVISION
uv run python -m yt_playlist.core.preparation.checkpoint_publication candidate-checkpoints /absolute/path/to/library.db --revision REVISION
```

Publication verifies exact source bytes, typed fields and current references in
a bounded transaction. Current and reopened readers discover the selected
generation dynamically. Published rows and originals remain immutable. Source
or group-reference writes invalidate selection immediately; compatible runtime
writers keep the original settings current, and readers use them until another
explicit reconciliation/publication. Background results reject consumed inputs
that changed. Native historical owners take precedence if present; the retained
source also preserves the frozen upgrade route. No old storage is retired.
Runtime selection validity is separate from operational preparation counters:
continuing work does not invalidate a result merely by creating its own output
generation after selection was already withdrawn. Existing connections bind
revision tracking for newly installed schema definitions before their next
background snapshot; this reads definitions without scanning stored data.

Saved recording/artist content encoders and their five scalar admission tokens
on layouts 79–93 prepare separately:

```sh
uv run python -m yt_playlist.core.preparation.model_artifacts encoders /absolute/path/to/library.db --batches 16 --batch-size 16
uv run python -m yt_playlist.core.preparation.model_artifacts fingerprints /absolute/path/to/library.db --batches 8 --batch-size 1
```

The encoder job retains exact JSON text in its own file root under
`DATABASE.preparation/content-encoders`. Parsing, hashing and durable file
publication run outside SQLite transactions. Each bounded writer batch copies
individual file references and fences source revisions. The limits are 16 MiB
and 256 parts per encoder; scalar tokens have a 64 KiB budget. Null, blank and
empty encoders remain distinct, and old file generations survive source changes
and removal. Both jobs support `--status` and explicit `--retry`; failure of one
leaves the other and regular DDL usable. Neither job fits models or changes
runtime ownership. To publish both reconciled proofs together:

```sh
uv run python -m yt_playlist.core.preparation.model_publication /absolute/path/to/library.db --artifact-revision ARTIFACT_REVISION --fingerprint-revision FINGERPRINT_REVISION
```

Publication compares exact installed sources and verifies file integrity outside
SQLite transactions. The bounded cutover fences both revisions and rechecks the
finite file manifest before selecting encoders together with their admission
tokens. Published SQL references and file generations remain immutable. Old and
compatible writers retain settings; any model or token edit withdraws the entire
selection in the writing transaction, so readers use current source values until
explicit reconciliation/publication. Native historical owners take precedence,
and the retained source preserves the frozen upgrade route. No storage is retired.

Production model-trial decisions, listening monitors and held album assignments
on layouts 79–94 have independent preparation jobs:

```sh
uv run python -m yt_playlist.core.preparation.model_trials live-trial /absolute/path/to/library.db --batches 8 --batch-size 128
uv run python -m yt_playlist.core.preparation.model_trials listening-monitor /absolute/path/to/library.db --batches 32 --batch-size 128
uv run python -m yt_playlist.core.preparation.model_trials album-assignment /absolute/path/to/library.db --batches 8 --batch-size 128
```

Each job supports `--status` and explicit `--retry`. Exact source bytes and their
captured observation time remain archived with a 256 KiB budget per document.
Typed fields preserve the frozen v95 masks, policy/setup values, held assignment,
and original run identity. Missing runs stay unresolved without creating one.
Monitoring baselines retain all 4,096 possible ordered values in bounded member
batches. Earlier complete generations remain selected in staging until a new
generation is complete. Source edits and run changes invalidate preparation;
failures leave the other jobs and regular DDL usable. These commands do not fit
models, change runtime ownership, delete source proposals or retire storage.

Select each completed trial preparation revision independently:

```sh
uv run python -m yt_playlist.core.preparation.trial_publication live-trial /absolute/path/to/library.db --revision REVISION
uv run python -m yt_playlist.core.preparation.trial_publication listening-monitor /absolute/path/to/library.db --revision REVISION
uv run python -m yt_playlist.core.preparation.trial_publication album-assignment /absolute/path/to/library.db --revision REVISION
```

Verification checks exact archived text and observation time, every typed field,
run resolution and ordered baseline before the final writer reservation.
Publication selects a generation and preserves its immutable original and rows.
Preopened readers discover selection immediately. Compatible source updates and
run changes withdraw the affected selection; native model controls take
precedence after a historical upgrade. Failed preparation remains separate from
regular DDL and requires explicit retry. No command fits models, creates a missing
run or changes the recorded assignment. Old storage awaits separately gated
cleanup.

Saved Radio offers and historical manual autotune reports on layouts 79–94 have
separate preparation jobs:

```sh
uv run python -m yt_playlist.core.preparation.saved_outputs radio-seed /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.saved_outputs autotune-report /absolute/path/to/library.db --batches 8 --batch-size 128
```

Both support `--status` and explicit `--retry`. Originals up to 512 KiB retain
their exact bytes in a separate preparation file root, including unreadable
historical reports and distinct null/blank settings. Reports use only a captured
finite `ran_at`; preparation never supplies a replacement timestamp. Radio keeps
its menu identity, comparison run, all 1,000 possible ordered tracks and explicit
policy references. Unknown runs, catalog entries and policies remain unresolved.
Parsing and durable file writes happen outside SQLite transactions; member
batches retain earlier complete generations through interrupted replacement.
Source/reference edits invalidate preparation without preventing ordinary writes.
These jobs do not change runtime ownership, delete settings or retire storage.

Select each completed saved output independently with its prepared revision:

```sh
uv run python -m yt_playlist.core.preparation.saved_output_publication radio-seed /absolute/path/to/library.db --revision REVISION
uv run python -m yt_playlist.core.preparation.saved_output_publication autotune-report /absolute/path/to/library.db --revision REVISION
```

Publication verifies exact file bytes and all typed references before reserving
the final writer. Source and custody revision checks reject concurrent changes.
Readers discover selection dynamically; ordinary saves continue through installed
settings and withdraw only the affected selection. Reference retirement also
withdraws the seed offer without blocking the write. Native output owners take
precedence when installed. Published files and reference rows remain retained.
Explicit `saved_outputs JOB DATABASE --retry` restores a missing file from the
unchanged source without rewriting immutable subjects or policies. An existing
corrupt file must first be quarantined by the operator. Failed recovery remains
explicit and does not block regular schema changes or the other output job.

TV state on layouts 79–94 has five separate preparation jobs. Build the bounded
artist alias lookup first for artwork and artwork requests:

```sh
uv run python -m yt_playlist.core.preparation.tv_state aliases /absolute/path/to/library.db --batches 32 --batch-size 32
uv run python -m yt_playlist.core.preparation.tv_state pool /absolute/path/to/library.db --batches 8 --batch-size 128
uv run python -m yt_playlist.core.preparation.tv_state artwork /absolute/path/to/library.db --batches 8 --batch-size 128
uv run python -m yt_playlist.core.preparation.tv_state artwork-requests /absolute/path/to/library.db --batches 8 --batch-size 128
uv run python -m yt_playlist.core.preparation.tv_state visual-checks /absolute/path/to/library.db --batches 8 --batch-size 128
uv run python -m yt_playlist.core.preparation.tv_state continuations /absolute/path/to/library.db --batches 32 --batch-size 128
```

All accept `--status` and explicit `--retry`. Repeat bounded invocations until
prepared; artwork jobs require the alias index to be reconciled after alias or
redirect changes. Lookup preparation processes at most 32 aliases per batch and
keeps exact originals plus unique canonical matches, preserving exact-match
precedence and normalized ambiguity without scanning the library on a miss.
Each TV job retains exact source text and observation time with a 2 MiB budget
and up to 32 file parts. Frozen v95 field constraints remain in the additive
typed relations. Individual candidates, attempts, images, samples, receipt
identities and receipt items are checkpointed separately. Missing references
remain explicit, and no request timestamp is invented. File work runs outside
SQLite transactions; source/reference writes invalidate preparation. Earlier
complete generations and installed proposals remain retained and usable.
These commands neither contact providers nor change runtime ownership.

Publish each reconciled TV owner independently using its `prepared_revision`:

```sh
uv run python -m yt_playlist.core.preparation.tv_publication pool /absolute/path/to/library.db --revision REVISION
uv run python -m yt_playlist.core.preparation.tv_publication artwork /absolute/path/to/library.db --revision REVISION
uv run python -m yt_playlist.core.preparation.tv_publication artwork-requests /absolute/path/to/library.db --revision REVISION
uv run python -m yt_playlist.core.preparation.tv_publication visual-checks /absolute/path/to/library.db --revision REVISION
uv run python -m yt_playlist.core.preparation.tv_publication continuations /absolute/path/to/library.db --revision REVISION
```

Verification compares exact original bytes, observation time, every ordered
typed member and existing reference outside the final writer reservation. The
short publication transaction checks the source and custody revisions, then
selects one immutable generation. Preopened readers discover it; native v95
owners retain precedence. Continuing source/reference writes withdraw the
affected selection and keep serving compatible proposals until reconciliation.
Artwork result publication and request completion share one transaction; legacy
read-modify-write helpers reserve the writer before reading. Published members
and original manifests stay retained. If a retained file is missing, rerun that
preparation job with `--retry` to reconstruct the exact file from an unchanged
source; a failed recovery does not block regular schema changes. No publication
command contacts a provider or initiates playlist playback.

Paired model projections and clicked library pools on layouts 79–94 have
independent preparation jobs. Prepare comparison retention roots after the paired
outputs have reconciled:

```sh
uv run python -m yt_playlist.core.preparation.ranked_state pairs /absolute/path/to/library.db --batches 32 --batch-size 128
uv run python -m yt_playlist.core.preparation.ranked_state roots /absolute/path/to/library.db --batches 32 --batch-size 8
uv run python -m yt_playlist.core.preparation.ranked_state clicked /absolute/path/to/library.db --batches 32 --batch-size 128
```

Repeat bounded invocations until prepared; all accept `--status` and explicit
`--retry`. Source text up to 128 MiB keeps its exact original file parts. A durable
plan captures complete ranked sets, scores, intensity/genre membership, policy
versions, display files, captured Radio vectors and ordered clicked parameters.
Missing observation times remain unknown; these capture plans cannot be admitted
directly as ordinary recommendation generations without a genuine creation time.
Parsing, identity resolution,
file publication and reads of the selected plan slice happen outside SQL
transactions. Later batches resume the captured plan without decoding the full
source again. After a missing plan file, explicit retry reconstructs the exact
plan and checks it against the committed checkpoint; damaged files require repair.
Source/reference changes withdraw the preparation proof without blocking ordinary
writes. After an upgrade adds genre redirects, rerun `clicked` to install capture
for that dependency and reconcile the earlier proof. All earlier generations,
installed proposal rows and original retention roots remain retained. These jobs
do not select new runtime owners or retire storage.

Once `clicked` is prepared, build its retained publication map in bounded batches
and select the completed stage separately:

```sh
uv run python -m yt_playlist.core.preparation.clicked_publication /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.clicked_publication /absolute/path/to/library.db --publish-stage STAGE
```

Publication verifies every original part, durable plan page, ranked candidate,
output file, policy reference and ordered genre label outside the final writer
transaction. The map retains each lane's exact generation before verification;
the final writer only rechecks the finite proof and selects that map. Existing
readers discover selection without restarting. Native clicked storage takes
precedence after a historical upgrade. Compatible writes and reference changes
withdraw selection while the installed source continues serving. Preparation
failures and missing files do not block regular schema changes. For an unchanged
captured lane, restore missing files with
`--repair-surface home_pool:wheelhouse`; changed references require reconciliation
and a fresh stage. Retained maps and originals await future explicit cleanup.

After both `pairs` and `roots` are prepared, retain and select their joint stage:

```sh
uv run python -m yt_playlist.core.preparation.pair_publication /absolute/path/to/library.db --batches 16 --batch-size 8
uv run python -m yt_playlist.core.preparation.pair_publication /absolute/path/to/library.db --publish-stage STAGE
```

Each step retains at most eight projection or comparison-root links. Root capture
also copies existing custody into separate `preparation:model-pair:` pins, keeping
the original group alias resolutions, recommendation generations and unavailable
context bytes. It does not resolve those aliases again. The pins keep referenced
artifacts alive even after a compatible writer releases an old source reference.
Complete source coverage, file bytes, ranked sets, vectors, all root archives and
pins are verified outside the final writer transaction. Selection commits the
projection generation and root publication together; concurrent source/reference
changes reject it. Compatible writes withdraw selection, and native paired
storage takes precedence. An unchanged projection can restore missing files with
`--repair-surface model_pair:RUN:ARM:FORMAT`. Staged pins, maps and originals remain
retained until the separately gated cleanup; failed or incomplete preparation
does not block regular DDL.

Retained taste checkpoints, ordered comparison baselines and explicit experiment
decisions on layouts 79–95 have independent preparation jobs:

```sh
uv run python -m yt_playlist.core.preparation.taste_controls checkpoint /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.taste_controls baseline /absolute/path/to/library.db --batches 32 --batch-size 128
uv run python -m yt_playlist.core.preparation.taste_controls decisions /absolute/path/to/library.db --batches 8 --batch-size 128
```

All support `--status` and explicit `--retry`. Exact originals up to 32 MiB,
including distinct absent/null/blank settings, remain retained with paged plans
in a separate preparation root. Checkpoints capture saved parameters, each
weight's original axis and observed interpretation without admitting new axes.
Baselines preserve ordered recording references, ranks, lanes and all traces.
Each of the at most 200 experiment decisions retains its time, power, basis and
individually owned evidence. Native recovery documents retain their 16 MiB limit.
Conflicting identities or invalid evidence fail explicitly while the original
setting stays usable. File work and identity resolution run outside SQL
transactions; later batches read their selected plan slice. Source/reference
edits withdraw the proof, and an earlier complete generation remains retained
through partial replacement. Rerun installation after historical upgrades add
genre redirects or taste axes so their capture is installed before resuming.
When a job reaches `prepared`, publish its reported revision separately:

```sh
uv run python -m yt_playlist.core.preparation.taste_control_publication checkpoint /absolute/path/to/library.db --revision REVISION
uv run python -m yt_playlist.core.preparation.taste_control_publication baseline /absolute/path/to/library.db --revision REVISION
uv run python -m yt_playlist.core.preparation.taste_control_publication decisions /absolute/path/to/library.db --revision REVISION
```

Publication checks exact source parts, every ordered typed member, all paged
plan files and each recovery document before taking the final writer reservation.
The final transaction checks finite revision and custody proofs and selects one
immutable generation. Readers return the exact original setting, including its
whitespace and numeric spelling; continuing writers keep the installed format
and withdraw only the affected selection. Reference edits also withdraw stale
selection. An upgrade adding optional taste-axis or genre-redirect owners falls
back to installed settings until explicit preparation captures and reconciles
them. Missing files require explicit `--retry`, which recreates the exact retained
files without changing published rows; damaged files require repair. Failed
preparation does not block regular schema changes. Native v96 owners take
precedence after a historical upgrade. Original settings and earlier generations
remain retained for the separately gated cleanup.

Import reports, saved playlist recipes and canvases on layouts 79–96 have three
independent document preparation jobs:

```sh
uv run python -m yt_playlist.core.preparation.collection_documents import /absolute/path/to/library.db --batches 8 --batch-size 128
uv run python -m yt_playlist.core.preparation.collection_documents recipes /absolute/path/to/library.db --batches 32 --batch-size 128
uv run python -m yt_playlist.core.preparation.collection_documents canvases /absolute/path/to/library.db --batches 16 --batch-size 128
```

All support `--status` and explicit `--retry`. Originals up to 32 MiB retain exact
bytes and captured creation times; native recovery documents keep their 16 MiB
ceiling. Import fields preserve presence masks, explicit nulls and known or
unresolved account IDs. Recipe execution fields and ordered, repeated genre/era
facets retain observed interpretations without admitting identities. Canvases
remain verbatim text with no JSON requirement. Recovery files have source-scoped
custody in a separate preparation root; destination numeric identities are not
assigned here. Parsing, file I/O and reference resolution run outside SQL
transactions, followed by bounded member batches using durable paged plans.
Source and reference edits invalidate proof while earlier complete generations
remain retained. Reinstall capture after historical upgrades add taste axes or
genre redirects. Sources and continuing writers remain authoritative; these
commands do not contact providers, select runtime owners or retire storage.

The single import report can publish independently after its preparation reaches
`prepared`:

```sh
uv run python -m yt_playlist.core.preparation.import_publication import /absolute/path/to/library.db --revision REVISION
```

Publication verifies every typed import field, its account attribution, exact
original parts, all plan pages and report/token recovery evidence outside the
final writer transaction. The finite final write selects an immutable retained
generation. Compatible readers return the exact original, while import workers
continue writing the installed setting and withdraw the selection atomically.
Runtime callers receive `account_id`; old-format writes retain `identity_id` in
the stored document. Missing/null account fields and unresolved IDs stay distinct
without admitting accounts. Failed or suspended preparation leaves regular DDL
usable. Explicit import preparation `--retry` can recreate exact missing files;
damaged files require repair. Native v97 jobs take precedence after historical
upgrade. Recipe/canvas publication remains a separate step; this command does not
select those owners or retire any source.

Recipes and canvases have their own retained publication maps. After the
corresponding preparation job is `prepared`, stage the current links in bounded
batches and publish the reported completed stage:

```sh
uv run python -m yt_playlist.core.preparation.collection_document_publication recipes /absolute/path/to/library.db --batches 32 --batch-size 128
uv run python -m yt_playlist.core.preparation.collection_document_publication recipes /absolute/path/to/library.db --publish-stage STAGE
uv run python -m yt_playlist.core.preparation.collection_document_publication canvases /absolute/path/to/library.db --batches 32 --batch-size 128
uv run python -m yt_playlist.core.preparation.collection_document_publication canvases /absolute/path/to/library.db --publish-stage STAGE
```

Each map batch retains at most 128 playlist/generation links, including unchanged
documents from earlier preparation revisions. Earlier maps remain immutable
across replacement and deletion. A source/reference change requires a new map
after reconciliation; incomplete stages remain restartable. Publication compares
every source, original part, recovery file, plan page, typed execution field,
ordered facet and creation time outside the final writer reservation. Only the
finite proof and selection are written at cutover. Preopened readers discover
the selected map, including playlist-list timestamps. Continuing recipe/canvas
writes withdraw the affected owner's selection and keep the installed documents
usable; native v97 owners take precedence.

Captured members become immutable as each map batch commits, including before
final publication. If verification finds damaged SQL rows, rewrite the unchanged
source through its compatible writer and prepare a fresh revision. Missing files
can be reconstructed for one unchanged source without rewriting captured rows:

```sh
uv run python -m yt_playlist.core.preparation.collection_document_publication recipes /absolute/path/to/library.db --repair-source PLAYLIST_SCOPE
```

Use `canvases` for canvas recovery. Failed file work or an incomplete map does not
hold up regular schema changes. These commands retain sources and contact no
provider.

Legacy playlist-GC retries and Spotify transfer receipts on layouts 79–97 have
independent preparation jobs:

```sh
uv run python -m yt_playlist.core.preparation.workflow_receipts gc /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.workflow_receipts playlists /absolute/path/to/library.db --batches 32 --batch-size 128
uv run python -m yt_playlist.core.preparation.workflow_receipts albums /absolute/path/to/library.db --batches 16 --batch-size 128
```

All support `--status` and explicit `--retry`. Exact originals up to 32 MiB remain
retained; recovery files keep their 16 MiB ceiling and source-scoped custody.
GC targets preserve captured account/playlist IDs, retry counts, deadlines and
failure evidence. Transfers preserve fingerprints, creation markers, remote
destinations, completion/order flags and all ordered video slots, including nulls
and repetitions. Repeated legacy fingerprints keep their original positions and
an explicit duplicate witness. Existing accounts/scopes are observed without
creating identities; missing references remain unresolved. File work precedes
bounded member transactions and resumed batches use durable paged plans. Source
and reference writes invalidate proof; failures retain earlier complete staging.
These commands do not change runtime ownership, delete sources, advance remote
operations or contact providers.

After a workflow job reports `prepared`, retain its map and explicitly publish
the returned stage. Each receipt family has its own selection:

```sh
uv run python -m yt_playlist.core.preparation.workflow_publication gc /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.workflow_publication gc /absolute/path/to/library.db --publish-stage STAGE
```

Use `playlists` or `albums` for the transfer families. Exact originals, typed
receipts, paged plans, ordered nullable slots, and recovery files are verified
before the bounded publication transaction. Readers opened before preparation,
including GC prefix reads, discover the selected map. Physical v98 owners take
precedence. Ordinary settings writers remain compatible and withdraw the affected
selection; reconcile the job and publish a new stage after continuing writes.
Account or playlist-scope changes also require reconciliation. Earlier maps and
all original settings remain retained.

If an unchanged receipt loses a file, restore its exact source-scoped files
without rewriting captured SQL members:

```sh
uv run python -m yt_playlist.core.preparation.workflow_publication playlists /absolute/path/to/library.db --repair-source SETTINGS_KEY
```

Re-publish its stage after recovery. A changed source must be reconciled first;
failed preparation requires its explicit `--retry`. Neither failed file work nor
an incomplete map blocks regular DDL. Publication makes no remote calls and does
not create an account, playlist scope, or remote operation.

Action-ledger custody on layouts 79–97 has an independent preparation job:

```sh
uv run python -m yt_playlist.core.preparation.action_ledger /absolute/path/to/library.db --batches 32 --batch-size 128
```

It supports `--status` and explicit `--retry`. It retains original action IDs,
exact document text (including plain draft signatures/evidence), captured times,
undo links, intent digests and observed sequence high-water marks. Typed targets,
steps, resolutions and ordered placements observe existing identities without
creating them. Historical pending deletions remain uncertain and require readback.
Verified backups up to 64 MiB retain exact bytes in separate preparation custody;
missing historical files remain explicit. Recovery documents keep their 16 MiB
ceiling, with a 32 MiB total source budget. File work precedes bounded SQL member
batches and resumed work uses durable paged plans. Source/reference edits withdraw
proof; earlier complete generations stay retained. This stages recovery without
executing or selecting runtime owners. Once prepared, install its immutable map
in bounded batches, then verify and select it separately:

```sh
uv run python -m yt_playlist.core.preparation.action_publication /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.action_publication /absolute/path/to/library.db --publish-stage STAGE
```

Publication verifies complete source coverage, exact original text, typed
targets, undo links, ordered placements, steps, durable plans and required files
outside the writer transaction. It rechecks external backups: changed bytes or
a newly available historical backup require reconciliation; a missing original
can use its verified retained copy. Final cutover fences continuing source and
reference writes, including changes to the action sequence high-water mark.
Publication never writes that sequence or reassigns action IDs. Installed SQL
writers remain authoritative and withdraw the selection on continuing updates.
Selected readers retain the external action document formats, redirect verified
backup references to custody, and keep historical pending deletions uncertain.
After a write withdraws selection, an exactly unchanged undo document can still
use its previously published backup custody. Current SQL fields remain current;
a changed undo document does not inherit an older projection. This also covers
an interruption after an undo claim and before its first progress save.
Physical v98 action owners take precedence; earlier maps and guards stay retained.

Exact unchanged sources can recreate their original, recovery and plan files:

```sh
uv run python -m yt_playlist.core.preparation.action_publication /absolute/path/to/library.db --repair-source ACTION_ID
```

Backup repair requires its retained copy or an original matching the captured
digest. Loss of both requires restoration. Changed source documents must be
reconciled first; failed preparation requires explicit `--retry`. None of these
file failures blocks regular DDL. Publication makes no provider calls and admits
no remote execution.

TV append and Radio append/replacement custody have separate jobs for their
genuine installed source owners:

```sh
uv run python -m yt_playlist.core.preparation.append_operations tv_legacy /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.append_operations radio_legacy /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.append_operations tv_native /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.append_operations radio_native /absolute/path/to/library.db --batches 16 --batch-size 128
```

Use `tv_legacy` on layouts 79–96 and `tv_native` on layout 97; use `radio_legacy`
on layouts 79–93 and `radio_native` on layouts 94–97. All support `--status` and
explicit `--retry`. After a historical owner replacement, use its new job; the
old checkpoint never implies capture on the replacement table. Originals retain
exact JSON or verified native recovery-file bytes in separate custody. Typed
operation fields preserve observed sent state, receipts, revision, role presence,
empty allowed queues, ordered repetitions and captured times. Preparation assigns
no missing session, account, recipe or operation identity. Durable paged plans
keep file work outside writer reservations and bound SQL member batches. Source
and account/scope edits invalidate proof; earlier complete generations remain
retained. These commands do not select runtime owners or advance remote work.

After the relevant job reports `prepared`, retain its immutable map and publish
the returned stage explicitly:

```sh
uv run python -m yt_playlist.core.preparation.append_publication tv_legacy /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.append_publication tv_legacy /absolute/path/to/library.db --publish-stage STAGE
```

Use the same job name as preparation. Full file, typed-member and source-coverage
verification runs outside the final writer transaction. TV recipe and Radio
session readers discover the selected operation map dynamically; installed
writers retain their existing format and session revision rules. Continuing
source/account/scope writes withdraw selection before reconciliation. Ordered
duplicate items retain their own metadata, and missing/empty/null roles remain
distinct. Legacy selections retire when their source owner is replaced; install
and prepare its native-format job separately. Physical v98 operation owners take
precedence. Earlier maps and original source storage remain retained.

Restore files for an unchanged source without rewriting captured members:

```sh
uv run python -m yt_playlist.core.preparation.append_publication tv_legacy /absolute/path/to/library.db --repair-source SOURCE_KEY
```

Use its numeric recipe ID for `tv_native`, episode ID for `radio_native`, or
`radio_current_session` for `radio_legacy`. Re-publish the stage after repair.
Changed sources require reconciliation; failed jobs require explicit `--retry`.
Incomplete publication and failed file work do not block regular DDL. These
commands contact no provider and admit no remote operation or missing identity.

Album row and frozen-share preparation on installed layouts 79–98 run separately:

```sh
uv run python -m yt_playlist.core.preparation.album_occurrences /absolute/path/to/library.db --batches 32 --batch-size 32
uv run python -m yt_playlist.core.preparation.album_shares /absolute/path/to/library.db --batches 16 --batch-size 128
```

Both support `--status` and explicit `--retry`. Row batches contain at most 32
source rows and reject oversized fields instead of truncating evidence. Exact
originals retain full versus incidental membership, absent positions, original
recording references, fetch completeness and nullable captured times. Existing
catalog entries and provider aliases supply observed metadata; preparation does
not admit missing identities. Shares retain exact document text, ordered repeated
video choices and unresolved videos in independent file custody. Durable paged
plans keep file work outside writer reservations and bound member transactions.
Source and reference edits invalidate proof; earlier originals and share
generations remain retained. The installed album writers continue throughout
preparation and publication.

After row preparation finishes, build and select the occurrence projection:

```sh
uv run python -m yt_playlist.core.preparation.album_publication /absolute/path/to/library.db --batches 16 --batch-size 32
uv run python -m yt_playlist.core.preparation.album_publication /absolute/path/to/library.db --publish-stage STAGE
```

Copying retains at most 32 rows or group summaries per transaction. Verification
outside writer reservations checks every exact original, typed reference and
group against the installed source. Only nonempty full captures with matching
recorded completeness and counts serve as complete tracklists. Known captured
order must cover every position; unknown and mixed order remain explicit.
Incidental observations and partial captures stay retained separately. Repeated
occurrences retain their positions and original row IDs.

Preopened readers select the projection within their query snapshot. Continuing
source/catalog/alias writes withdraw it in the same transaction and fall back to
the installed source until reconciliation and another publication. Activation
also invalidates recommendation inputs. Interrupted copying resumes its durable
cursor; interrupted selection rolls back. Changed inputs require preparation
again, and a failed preparation requires explicit `--retry`. Original SQL rows,
immutable completed stages and earlier generations remain retained. Physical v99
owners take precedence after a historical upgrade. Routine schema changes do
not run this conversion or wait for its completion.

Frozen-share readers can independently select a verified retained map:

```sh
uv run python -m yt_playlist.core.preparation.album_share_publication /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.album_share_publication /absolute/path/to/library.db --publish-stage STAGE
```

Verification compares complete source coverage, exact original bytes, ordered
typed choices, observed references and durable plans outside the final writer
transaction. Bounded map batches retain immutable custody before a short final
selection. Preopened readers discover the map dynamically. The installed
tracklist-completeness check still gates shares; publishing choices does not
declare an incomplete album complete. Provider aliases keep their existing
fallback order. Continuing source/catalog/alias writes withdraw selection in
their transaction and retain the legacy writers until reconciliation. Physical
v99 owners take precedence after a historical upgrade; earlier map custody stays
retained. Missing original or plan files for an unchanged source can be rebuilt
without changing captured SQL rows:

```sh
uv run python -m yt_playlist.core.preparation.album_share_publication /absolute/path/to/library.db --repair-source BROWSE_ID
```

Re-publish the stage after repair. Changed sources require reconciliation;
failed jobs require explicit retry. Neither failure blocks regular schema changes.

Discovery subject, origin and retained classification preparation has separate
commands from candidate explanation preparation:

```sh
uv run python -m yt_playlist.core.preparation.discovery_subjects rows /absolute/path/to/library.db --batches 32 --batch-size 16
uv run python -m yt_playlist.core.preparation.discovery_subjects classifications /absolute/path/to/library.db --batches 32 --batch-size 16
uv run python -m yt_playlist.core.preparation.discovery_artists artists_legacy /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.discovery_artists artists_classified /absolute/path/to/library.db --batches 16 --batch-size 128
```

Use `rows` on layouts 79–100, `classifications` on 86–100, `artists_legacy`
on 79–85 and `artists_classified` on 86–100. All support `--status` and explicit
`--retry`. Row batches contain at most 16 sources and retain exact originals,
nullable clocks, typed observed origins and unresolved impression keys. Existing
offer counts remain separate from visible-visit receipts; publication must
deduplicate canonical visits before calculating any unverified residue. Candidate
explanations retain exact text and their native display interpretation in separate
file custody. Malformed text stays explicit; non-finite recovery output fails.
Paged plans keep file work outside writer reservations. Source/reference edits and
key renames withdraw proof while previous custody remains retained. These jobs do
not create identities, publish owners, alter serving counts or remove source data.

Artist explanation files have their own bounded publication after the matching
candidate job reconciles:

```sh
uv run python -m yt_playlist.core.preparation.discovery_artist_publication artists_classified /absolute/path/to/library.db --batches 16 --batch-size 8
uv run python -m yt_playlist.core.preparation.discovery_artist_publication artists_classified /absolute/path/to/library.db --publish-stage STAGE
```

Use `artists_legacy` for layouts 79–85. Verification checks exact source coverage,
all original parts, captured scalar fields, identity resolution, explanation bytes
and durable plans outside the writer reservation. Each map batch plans at most
eight keys, capped at 4 KiB each, before taking the writer; oversized keys fail
explicitly with their prepared originals retained. Final selection fences source
and reference changes. Artist display reads use the selected file projection;
installed identities, genres and offer-counter precision stay consistent with
the other discovery owners. Canonical subject merging and visible-visit/legacy
offer reconciliation remain a separate conversion. Compatible writes withdraw
selection in their transaction and retain earlier file custody. Native v101
owners take precedence. Missing files do not block regular DDL; rebuild an
unchanged source explicitly with `--repair-source ARTIST_KEY`. Changed sources
require reconciliation. No source rows, migration history or files are retired.

Canonical discovery selection follows the matching artist file publication and
reconciled row/classification jobs:

```sh
uv run python -m yt_playlist.core.preparation.discovery_publication classified /absolute/path/to/library.db --batches 32 --batch-size 16
uv run python -m yt_playlist.core.preparation.discovery_publication classified /absolute/path/to/library.db --publish-stage STAGE
```

Use `legacy` on layouts 79–85 and `classified` on 86–100. Each staging transaction
retains at most sixteen inputs, with a 512 KiB encoded row limit checked before
the writer reservation. Full verification runs outside that reservation and
checks installed originals, source coverage, typed references, representative
recordings, classifications and the selected artist files. A short final fence
selects one generation. Explicit views deduplicate canonical visits and origins;
they keep verified offer counts separate from unverified legacy residue. Display,
starter choices, scan scheduling, provenance and cleanup read that same selection.
Cleanup resolves installed deletion targets before writing, including multiple
captured aliases for a canonical subject or source. Continuing writes retain the
installed format and withdraw selection in their transaction; reconcile the jobs
and stage again to publish a successor. Incomplete or failed preparation leaves
ordinary startup and installed reads usable. Earlier generations, originals and
files remain retained. Native v101 owners take precedence after the explicit
historical upgrade route, with retained custody still validated by regular DDL.

Historical field-contract auditing is separate from future-write guards:

```sh
uv run python -m yt_playlist.core.preparation.domain_contracts youtube_items /absolute/path/to/library.db --batches 32 --batch-size 32
uv run python -m yt_playlist.core.preparation.domain_contracts youtube_items /absolute/path/to/library.db --status
uv run python -m yt_playlist.core.preparation.domain_contracts youtube_items /absolute/path/to/library.db --verify
```

Choose an available owner from the command's finite owner list on layouts 79–107.
Installation adds guards without scanning or replacing the installed owner. The
explicit worker captures at most 32 rows per transaction, preserving SQLite
storage types and exact numeric values. Invalid historical fields remain in
independent original custody and block verification; they do not hold up regular
schema changes. Failed batches require `--retry` after repair. Status and verify
open read-only; verify checks the reconciled revision and indexed blockers.
These commands do not stamp v104, rebuild populated tables or select runtime
owners. Existing CHECK constraints remain installed where already present.

The exact earlier v86/v88 genre definitions have a bounded regular repair too.
It repairs the known routing programs, adds archive fields without inventing
historical values and guards future taste-axis writes. It preserves the installed
axis table and invalid historical rows. Historical axis auditing runs separately:

```sh
uv run python -m yt_playlist.core.preparation.installed_genre_contract /absolute/path/to/library.db --batches 32 --batch-size 32
uv run python -m yt_playlist.core.preparation.installed_genre_contract /absolute/path/to/library.db --status
uv run python -m yt_playlist.core.preparation.installed_genre_contract /absolute/path/to/library.db --verify
```

The audit supports layouts 88–107 and preserves exact SQLite field types and
values. Invalid history blocks this audit's verification while regular DDL and
valid ordinary writes continue. Repair the affected source before `--retry`;
earlier captured values remain retained. The three optional lookup indexes
missing from the earliest genre layout remain absent rather than indexing a
populated serving owner during regular advancement. The frozen explicit v92
replay remains available and does not lose the independent audit or guards.

The frozen-share membership fence and historical audit on layouts 99–107 also
run independently:

```sh
uv run python -m yt_playlist.core.preparation.frozen_shares /absolute/path/to/library.db --batches 32 --batch-size 32
uv run python -m yt_playlist.core.preparation.frozen_shares /absolute/path/to/library.db --status
uv run python -m yt_playlist.core.preparation.frozen_shares /absolute/path/to/library.db --verify
```

Installation rejects future appends, replacements and reordered choices before
auditing old captures. It leaves earlier immutable rows intact, including damaged
history. Batches retain exact snapshot/member originals and reconcile indexed
membership counters. Verification reports missing, extended or reordered capture
membership without rescanning source history. Failure requires explicit `--retry`;
the guard and ordinary writers remain usable. Empty captures, ordered repetitions
and unresolved original source references remain explicit. These commands do not
stamp v105 or change runtime ownership.

Calendar-scope lookup indexes on layouts 100–107 can be prepared without a full
`CREATE INDEX` over a populated serving table:

```sh
uv run python -m yt_playlist.core.preparation.calendar_indexes /absolute/path/to/library.db --batches 32 --batch-size 32
uv run python -m yt_playlist.core.preparation.calendar_indexes /absolute/path/to/library.db --status
uv run python -m yt_playlist.core.preparation.calendar_indexes /absolute/path/to/library.db --publish REVISION
```

Use the reconciled input revision reported by status for `REVISION`. Lookup
tables and their covering indexes start empty; each preparation batch visits at
most 32 source rows. Before selection, verification compares every lookup value
and complete coverage with the installed sources in read-only batches. A custody
revision rejects lookup changes during verification; source journals separately
fence continuing input writes. Publication then selects the lookups in a bounded
transaction. Earlier prepared lookups require this verification before selection.
Before publication, calendar queries use the installed owners. Afterwards,
continuing source inserts, updates, resolution and deletion maintain the matching
lookup row in the same transaction, including while a later explicit batch is
suspended or failed. Reads still join the authoritative source rows. No original
listening data, account or migration history is changed. Failure requires
`--retry`; status opens read-only. This does not stamp v106 or build its native
indexes synchronously on populated owners.

Preparation sources remain authoritative throughout these jobs. Source changes
enter durable journals. Reference changes invalidate proof and request a fresh,
finite bounded rescan, including later keys already copied through the journal.
Workers which plan files outside SQLite capture the boundary in their read
snapshot and publish it only after checking that the input revision is unchanged.
Interrupted or failed batches retain their prior checkpoint and boundary.

Selected genre labels on installed layouts 79–85 and imported deletion readback
decisions on layouts 98–106 have independent bounded jobs:

```sh
uv run python -m yt_playlist.core.preparation.genre_classifications /absolute/path/to/library.db --batches 16 --batch-size 128
uv run python -m yt_playlist.core.preparation.deletion_readback /absolute/path/to/library.db --batches 16 --batch-size 128
```

Both accept the same `--status` and explicit `--retry` controls. Genre staging
reads labels within a 4096-byte limit, retains exact originals, distinguishes
null/blank/unknown/ambiguous values, and rechecks reference changes without
creating identities. Once reconciled, build and independently verify its separate
runtime selection:

```sh
uv run python -m yt_playlist.core.preparation.genre_publication /absolute/path/to/library.db --batches 16 --batch-size 16
uv run python -m yt_playlist.core.preparation.genre_publication /absolute/path/to/library.db --publish-stage STAGE_ID
```

Each staging transaction copies at most 16 typed labels with a 64 KiB row budget.
Source comparison, reference validation and coverage checks run outside the final
selection transaction. Private readers use the selected labels in catalog,
listening, discovery and album projections; explicit user overrides keep their
precedence. Compatible label, catalog and alias writes withdraw the selection
until preparation and publication reconcile. Retained originals and selected
generations stay immutable, and native v86 classifications take precedence after
an independently requested historical upgrade. Neither command advances the
historical migration ledger or removes installed label owners.

Deletion staging
retains each observed journal revision and its readback decision; it never calls
a provider or rewrites the active action ledger. Runtime guards already require
readback for ambiguous imported pending deletions before forward playlist writes,
including while preparation is absent, suspended or failed. Modern intents with
a recorded unsent transport state remain usable. These jobs neither stamp their
historical conversions complete. Deletion staging does not activate destination
owners.

Scheduling/TV work has an explicit verified publication step. After its work job
reports `prepared`, pass that job's `prepared_revision` (shown as `0` here):

```sh
uv run python -m yt_playlist.core.preparation.work_publication /absolute/path/to/library.db --revision 0
```

Publication checks the exact retained inputs and both typed checkpoints under
one writer reservation. It atomically admits those typed tables for runtime
reads, including readers opened before publication. Continuing writes through
the retained documents validate and update the admitted owner in the same
transaction. New repository writes use that same boundary. The original
publication inputs remain immutable, and the compatible documents remain
available for the explicit historical upgrade route. Interruption rolls back
the entire publication; rerunning the command is idempotent. Startup verifies
an active publication's definitions without scanning the library. This completes
publication for these two work checkpoints only; publication for the other
preparation jobs remains outstanding except for Home offer deduplication:

```sh
uv run python -m yt_playlist.core.preparation.offer_publication /absolute/path/to/library.db --revision 0
```

Use the `prepared_revision` reported by the `home-offers` job. This publication
checks the original document, epoch and complete signature set, then admits the
typed owner atomically. Continuing retained-document writes maintain that owner
and reject invalid windows. Existing and reopened connections claim signatures
under one writer reservation, preserving deduplication across publication.
The original publication input stays immutable, and the retained current
document remains compatible with the explicit historical upgrade.

Prepared recommendation policies share a content-addressed configuration body across
ranker, vibe, lane and genre variants. Existing full JSON policies remain readable.
Background preparation collects unused policies after a one-hour grace period,
preserving impression references and policy IDs in prepared/session data. Unused
shared policy storage has admission limits of 64 MiB and 131,072 variants; an individual
batch is capped at 8 MiB and 16,384 variants. These admission limits apply to new
shared records; legacy full JSON policies are collected in bounded batches.
If unused shared records, including newly expired served
evidence, exceed the limits, preparation defers while maintenance catches up.
A rejected batch retains the published pools.
Online collection targets 8 MiB each for policy rows and orphan bodies per pass,
including the last row that crosses the target, with at most 4,096 of each.
Prepared policy writes reserve at least 256 MiB of filesystem headroom. Served
attribution is protected independently of those disposable-data limits.

Startup applies bounded schema changes separately from historical preparation.
Installed v0.1.18 libraries (schema 64) and the explicitly supported development
schema 93 start one background database rebuild. It creates the current schema
once, copies and transforms retained data into it, verifies the result, compacts
the candidate and installs it through the shared replacement/recovery boundary.
The source's applied migration history is preserved as provenance in
`database_rebuild_source_history` and `database_rebuild_source_changes`;
the rebuild does not replay bulk historical migrations against the source.

The implementation lives in `core/db_rebuild/`: `job.py` owns recovery custody
and phase order, `build.py` owns atomic batch bookmarks and candidate construction,
and `install.py` owns closed-handle replacement and rollback. `destination.py`
selects the current schema and canonical domain converters. The released and
development importers admit only their two declared source layouts. Domain
readers, writers and retention policies remain with their existing owners.

During construction the original serves reads while library writes are paused;
released-schema compatibility preserves saved-playlist playback. Playback
observations use the durable inbox described below. Cutover briefly fences
requests while handles close and the installed database reopens. Failure retains
the recovery source and candidate. Restart can resume committed batches only
when the source and destination code still match the recorded contract. Changing
the destination contract requires a fresh candidate.

An explicit local `POST /storage-maintenance/rebuild` starts a fresh attempt;
`POST /storage-maintenance/rebuild?resume=true` requests an eligible interrupted
build. Status and phase details are available at
`GET /storage-maintenance/status`. These requests use the same worker and
installer as the automatic upgrade. Retained-copy compaction below is the
separate operation for preserving an already installed format.

Retained-copy compaction is an explicit maintenance request. Browsing, starting
and tuning radio, and existing playback remain
available during repair. Confirmed ratings and playback observations are captured
in a durable inbox and applied locally after repair; receipts prevent duplicate
application after interruption. Replaying observations never repeats remote rating
or Next commands. Playlist changes, syncing, imports and settings changes return
a retry message. New recommendation
impressions and radio session checkpoint writes wait until repair finishes.
Radio uses an in-memory session during repair. After repair, minimal generated
playlist provenance is saved so subsequent plays stay excluded from taste learning.
Recommendation, enrichment and sync workers wait until repair finishes or safely
defers. Normal recording then resumes, including when final compaction is deferred.
Progress and deferral reasons remain in the logs and maintenance status.
The top storage banner says “Library updates are paused.” while writes are paused.
It stays on one line, including in narrow windows with enlarged text. A low-space
warning remains visible after recording resumes.

Recovery backups include the pending playback inbox and its owned observation
files, together with a consistent snapshot of the main database and its replay
receipts. When copying a stopped library manually, preserve its playback-feedback
sidecar database, lock file and observation directory along with the other owned
files.

To request retained-copy compaction in a running application, send a local
`POST /storage-maintenance/compact`. For a stopped library, use:

```bash
uv run python -m yt_playlist.core.db_rebuild.job /path/to/state.db \
  --scratch /existing/directory/with/space
```

The source stays unchanged while retained rows are copied directly into a new
database in its supported installed format. Compaction does not finish pending
historical conversions. Only demonstrably disposable recommendation generations
and unowned pools are omitted; all other personal, historical, group and
preparation owners remain. Complete candidate pools retain their full eligible
coverage. Hidden row IDs, migration history and sequence high-water marks remain
exact. Secondary indexes and publication guards are restored before installation.

Compaction retains an independently verified SQL backup and external-file
manifests. Installed native and preparation files, including unfinished plan
graphs, stay in place with their SQL coordinates unchanged. File-retention locks
remain held throughout copying and replacement. Captured sample JSON
keeps its exact bytes and collection identity; missing or corrupt required input
prevents a verified recovery point. SQL row verification,
schema comparison, full integrity and foreign-key checks precede installation.
Scratch may be on another filesystem: a verified, synced SQL sibling must first
be prepared beside the destination database.

The app and preparation commands hold shared lifetime-handle leases. Installation
closes the serving connection and requires exclusive admission after every other
participating handle closes. Pinned handles abort installation and resume the
original library. SQLite performs its checkpoint and WAL retirement before the
closed database is renamed; compaction never deletes a live WAL. The original
database remains recoverable beside the installed copy. No physical reclamation
is claimed while that original and its verified backup are retained for acceptance.

`state.db.compaction.json` records progress, capacity, timings, file custody and
recovery coordinates. Startup resolves interrupted work before opening the main
database. Pre-install interruptions retain the original; interrupted renames roll
back to the verified original; a durably installed candidate is checked before
admission resumes. Capacity checks include SQL images, index/journal allowance,
owned files and a 256 MiB reserve on each filesystem. Failure preserves recovery
files and reports the reason; retry compaction explicitly after resolving it.
Keep the installed library's existing external-file directories. Independent
acceptance permits a separate, reviewed cleanup of recovery copies.

The shared catalog separates a musical recording (`recordings.id`) from its playable
uploads (`youtube_items.video_id`). New uploads get separate provisional recordings;
matching titles, MusicBrainz IDs, or ISRC candidates do not merge them automatically.
Verified associations retain their source and evidence. Playlist operations and
YouTube ratings still address exact uploads. Missing-video album entries remain
metadata with explicit album links and are excluded from playable recommendations.

Library and discovery memberships reference this catalog. One recording enrichment
queue retains full provider responses, identifiers, and scoped findings for either
membership. Discovery expiry removes candidate membership while preserving catalog
and enrichment evidence. Legacy numeric URLs/actions resolve through
`legacy_track_refs`; raw interaction keys and ambiguous evidence remain retained.
Failed lookups retain per-recording/provider progress and exponential retry delays (30 seconds to
one hour). Provider outages and throttling persist cooldowns and honor longer `Retry-After` values.
Untouched work precedes retries; successful answers are reused until that pass completes. The
enrichment status panel shows saved retry deadlines and errors, including after a restart.
Provider enablement, availability and effective credential changes schedule targeted coverage passes
for existing recordings, including those with complete metadata. Other providers' answers and user
selections remain intact. Disabled/unavailable providers remain distinct from successful, no-match,
pending and retry outcomes; pending provider coverage contributes to the visible queue count.
Artwork outcomes are saved separately as found, confirmed absent, or retryable. Optional archive
retries retain release IDs and respect response-cache deadlines, use bounded backoff, and survive
restarts. Each worker drain handles at most one due artwork retry alongside metadata work. Useful
covers are preserved, and artwork failures do not reopen a completed metadata pass.
Derived vectors are rebuilt in identity version 2, and incompatible model rollback
artifacts remain available for inspection rather than being loaded into that space.

Live catalog fields and exact source-entry IDs belong to `core.catalog_identity`; saved-key
rewrites belong to `core.catalog_state`. Numbered migrations retain their own frozen definitions.
Unmarked saved JSON uses runtime format 1; rewritten dictionaries carry `catalog_state_version`.
New serialized formats need a versioned reader/rewriter and stamped writers. Unknown future formats
are preserved during live rekeying. Schema changes use a new migration without changing old upgrades.

### The stack

TuneConsole is a single local process.

ASGI lifespan owns periodic sync, recommendation/enrichment workers, the idle monitor, and the serial
radio executor. Restart, native quit, and idle shutdown close the same work-admission gate before
checking activity. Admitted builds and mutations remain tracked through completion, including after
request cancellation; queued radio applies are cancelled during shutdown. Lifespan requests cooperative
worker stops and allows 30 seconds to drain, reporting a shutdown failure if work remains. App factories
and tests can explicitly disable periodic work with `create_app(..., background_work=False)`.

The radio controller serializes playlist changes outside the bridge response pump. Ordered remote
readback distinguishes complete, partial, and failed writes; only confirmed uploads enter applied
state. Partial queues remain retryable and appear in the radio card and `/bridge/status` diagnostics.

Playlist snapshot errors include a validation stage and a bounded envelope report:
fixed schema names, value types, counts capped at 1,000, and at most eight sampled
actions/items. These distinguish missing continuation envelopes, wrong contents
types, unsupported renderers and parser failures without logging response bodies,
unknown field names, titles, account data or continuation tokens. The read-only
`/playlist/{local_id}/snapshot-report` also returns the failed page diagnostics.
The two Radio page-two failures observed on 2026-10-23 recovered their next-track
queue within about five seconds. Their original payloads were not retained, so
their upstream cause remains unknown. Collect the new stage/envelope evidence if
they recur before changing parsing or retry policy; an incomplete read never proves
that songs are missing and never authorizes replaying an uncertain write.

Takeout and Spotify history imports run as tracked jobs outside the ASGI loop, sharing the library
sync lock. The import form polls phase and terminal status; disconnecting a request leaves its job
owned until processing finishes, so shutdown waits for history writes, repair, and discovery seeding.

- **Python / FastAPI**: an ASGI app served by uvicorn (`--reload` supported). Routes return
  server-rendered HTML, not JSON.
- **Jinja2**: every page and partial is a server-rendered template.
- **HTMX**: hypermedia, baby.
- **Alpine.js**: for client side interactivity that HTMX doesn't cover. Sortable.js handles drag-to-reorder and d3-force draws the Clusters graph.
- **SQLite**: the whole library, play history, and model state live in one local SQLite file.
  `store.py` composes per-domain DAOs (the `repos/` package) behind a single connection.
- **ytmusicapi**: builds the YouTube Music (InnerTube) requests and parses the responses. It does
  not make the network calls itself: a custom session routes each request through the browser
  extension, which applies your live session and returns the response. Frontend libraries are
  vendored locally, so nothing is fetched from a CDN at runtime.

Regular song views use the `song_menu` launcher in `web/templates/_partials/song_menu_button.html`.
`base.html` mounts one menu and playlist dialog from `_partials/song_actions.html`, backed by
`static/song-actions.js`. Add contextual actions there; specialized tools such as merge can keep
their own controls.

### The model

The recommender is local, CPU-only, and trained on your own library and listening history. Its
implementation lives in `src/yt_playlist/rec/` and has four main parts:

- **Representations.** `embed.py` maintains a collaborative track space from co-occurrence baskets
  (playlists, albums, artists, listening sessions, genre families, and decades). The default builder
  is PPMI plus truncated SVD; automatic model selection can select item2vec instead. A separate
  content space encodes genre, era, musical key, and enriched audio features. It supports taste modes, clustering, and
  cold-start candidates that are not yet in the library.
- **Taste and scoring.** `scoring.py` represents durable taste as one centroid per coherent playlist,
  weighted by how much that playlist is played. Candidate scores are the weighted fit across those
  contexts, then adjusted by genre, era, artist, popularity, and breadth preferences. This avoids
  collapsing a multi-modal library into one user vector.
- **Recent intent.** `transient.py` and `layers.py` derive decaying signals from recent plays, likes,
  dislikes, skips, and explicit mood feedback. Collaborative, session, and audio-space tilts affect
  ranking without replacing durable taste; repeated evidence can graduate into persistent facet
  weights. Machine-generated radio plays are excluded from taste evidence to prevent feedback loops.
- **Surfaces and discovery.** Each surface in `surfaces.py` owns its candidate pool, exclusions, and
  rotation policy. Out-of-library discovery uses YouTube Music catalog data and cached Last.fm edges,
  but TuneConsole's local models do the ranking. `rec_worker.py` coalesces rebuild requests, persists
  vectors, and materializes expensive proposals in the background so routes can serve the last good
  result from SQLite.
  Background radio collection is disabled unless `radio_discovery_enabled` is explicitly set to `1`;
  existing discovery tracks remain available to Fresh. Enrichment processes one ordered queue of
  library and discovery tracks and refreshes Fresh's prepared ranking and Home taste-mode buckets
  with the resulting genre, year, and audio metadata. Cached tracks retain release years for era
  filtering and descriptions. Fresh also works without enrichment or a fitted taste model:
  discoveries with no usable metadata or artist signal remain available after ranked candidates.
  Explicit genre requests still require a known matching genre.
  Failed provider requests remain queued; successful misses count as checks. Per-provider outcomes
  are retained in recording-linked attempt history with their input upload and full response.
  Discovery Pools shows saved metadata and genre, era, and metadata-gap charts. Legacy discovery batch stamps are
  requeued once because the old sink retained no provider history. Daily cleanup also expires
  never-shown tracks from their discovery date (30 days by default), while generated playlists
  protect their tracks. Expired tracks are removed from prepared recommendations too.
  Further artist, album, and source modeling is planned in [schema_changes_plan.md](schema_changes_plan.md).
  Album and artist views count tiles visible on Home's Discover tab once per visit.
  Historical background loads are retained
  separately and do not start the cleanup clock.

Automatic selection (`rec/model_selection.py`) begins after 14 days of history and 100 modeled
tracks. It fits alternatives in memory and requires at least 50 paired targets across two periods,
consistent gains, and a multiple-comparison-adjusted sign test before publishing a different setup.
Successful comparisons recur no more often than every 28 days and require 30 fresh listening tracks.
Model Health shows readiness, the selection result, and prospective top-20 prediction measurements.
Both setups save forecasts on the same candidate pool before exact-timestamped later plays arrive;
generated-only plays are excluded. Daily forecast refreshes happen during model updates. Measurements
for an applied selection continue through later checks that retain it. Restoring the exact previous
vector snapshot pauses selection until explicitly resumed; preferences and regular training are kept.

### Home recommendation audit

The Home configurator's **These mixes** tab shows your top 12 available genre families by listening,
with search for the rest. Its single action makes mixes from the selected genres, or shuffles
away from the current genres when none are selected. Successful changes close the configurator
to reveal the new mixes. Thin mixes search the full ranked library for the same genre family,
then fill with nearby styles if needed; artist and album caps still apply. Fresh songs stay in the
discovery pool, and Comfort stays limited to real listening favorites. Requests can offer fewer
cards when choices are limited, and explain when no
matching themes are ready. A request lasts for the current menu, including previews, until the next
normal refresh/rotation. It does not change taste weights or record dislikes. The **Preferences**
tab holds the existing sliders. The configurator opens as a modal with a backdrop and contained
keyboard focus; Escape or a backdrop click closes it.

`home-cards.jsonl` sits alongside `app.log` in the app's logs directory (normally
`~/.local/share/yt-playlist/logs` on Linux, or `$YT_PLAYLIST_HOME/logs` when overridden).
It rotates daily in UTC and keeps 30 archives. Logging starts when the updated app starts.

Each JSON line snapshots a successfully rendered Home card response: UTC time, request path,
rotation epoch, and each card's framing, displayed description, mode, recipe, genre/decade counts,
missing-metadata counts, and ordered track identities. Counts use the same metadata fallbacks as
the displayed description. They describe the offered tracks even if these differ from the recipe.
Normal loads, repeated previews, and refreshes are all recorded; use the path and epoch to separate
them when reviewing the week. These are server offers, not confirmed views or inferred rejections.
The audit is diagnostic only and does not feed the recommendation models.

### Recommendation response measurement

Fresh songs exclude discoveries with neither usable ranking evidence nor genre/year/audio metadata.
Metadata-bearing tracks may follow scored candidates as explicitly unscored fallbacks while a model
is unavailable. The cold rankability diagnostic reports scored and fallback counts separately.
Genre tilts use the same missing-metadata neutrality rule for cold and owned candidates: when genre
coverage is mixed, known positive weights are geometrically centered and missing genre stays neutral.
Hard mutes remain exclusions.
Genre mutes apply when recommendations are served, including cached Home mixes and warm tracks;
automatic learning leaves them in place until a manual unmute or reset.

The 90-day served ledger records final track recommendations from Home mixes, playlist completion,
Songs like, Clusters, and Road Trip drafts. Each impression preserves its final rank, lane/mode, available candidate score,
and serving policy fingerprint. Policy records retain effective parameters, preferences, exclusions,
recent evidence, and identities of the actual collaborative/content/artist/taste/mode models. Cached
candidate pools also retain their build policy, separately from the policy at rendering. Metadata
availability, ownership, artist/genre and last-play time are snapshotted when served; later enrichment
or adoption cannot change a historical coverage slice. Diagnostic previews and ordinary library or
search tables are excluded. Unchanged Road Trip progress polls count as a single draft offer.
These are server-rendered offers, not proof the browser saw every row.

The north-star metric is **rendered to played within 24 hours**. Completion, skip, repeat rate and
artist/genre variety are guardrails; listening depth and days since last play describe sustained
listening and staleness. Generated responses are measured separately from organic preference evidence.
Open, successful playback requests, successful saves, and promotions have their own event ledger;
a playback request is not proof playback started. Discovery conversion means a cold recommendation
was explicitly saved into the library or its generated playlist was promoted. Merely generating a
playlist does not count as conversion. Nothing in this measurement ledger trains the taste model.

Outcomes belong to the nearest preceding impression across all surfaces, within an inclusive 24-hour
window; report cohorts use `[start, end)`. Repeated events can credit an impression only once per
outcome. Saved video identities preserve skip/completion attribution after discovery cleanup.
Historical impressions without coverage/provenance remain unknown. Reports expose sample
sizes per slice, mark fewer than 100 impressions as collecting evidence, and never infer a comparative
verdict from observational data. Recall/projection metrics remain component diagnostics.

Projection recall uses five deterministic folds grouped by track identity. Each target is excluded
from its fold's ridge fit, content vocabulary, and audio normalization. Artists may cross folds;
precomputed artist features are disabled to avoid sharing their fitting history. The collaborative
embedding remains a fixed reference, so this measures metadata projection generalization rather than
end-to-end cold-start or unseen-artist performance. Reports retain eligible, scored, unencodable, and
insufficient-training counts, plus genre, era, and coverage slices. Each fit requires 20 usable tracks.

The Taste model health panel shows response and coverage summaries. `GET /taste/scorecard` returns
the full scorecard (optional Unix-second `start` and `end`, at most 90 days);
`GET /taste/served/{id}` returns an impression and its stored policy for inspection.

PPR defaults to a **live** 50/50 experiment with standard cosine ordering. Each final Home card and
its alternative ordering share a comparison ID, stable candidate fingerprint and actual cached PPR
solver configuration. The local, 90-day ledger records policy-assignment propensity (conditional on
the selected mode, not per-track inclusion probability), score inputs, rank and final DJ position.
Counterfactual rows never receive plays, actions or exit outcomes. Generated-playlist responses remain
separate from organic taste evidence. The old PPR JSONL writer and lane-only log are retired; existing
JSONL files are historical snapshots and are no longer consumed or appended.

`GET /taste/experiments` reports mature live exposure, acceptance, sustained listening and skips by
policy cohort, plus counterfactual counts. Wilson 95% intervals use epoch/mode clusters, pooling repeat
views; outcomes mature for 24 hours. `POST /taste/experiments/decision` accepts form fields `decision`
(`promote` or `retire`), `cohort` and `reason`. Promotion requires 100 mature clusters per live arm,
disjoint acceptance intervals, and conservative sustained-listening and skip guardrails. Shadow
evidence cannot authorize promotion. Retirement may stop an underpowered experiment, with that limit
recorded explicitly. Decisions retain the last 200 records locally. Share 0 means shadow, share 1
means retained PPR; an explicit retirement at share 0 also disables replay. These are conservative
observational comparisons of deterministic cohort assignment, not a claim of causal improvement.
