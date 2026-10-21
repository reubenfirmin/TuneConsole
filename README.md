# TuneConsole

A privacy-first tool that allows you to manage your YouTube playlists & albums, and find new songs to listen to. Includes a layered recommendation engine that you can fine tune. Also includes the ability to import from Spotify.

I built this because:

a) I find YouTube's recommendations OK but not great

b) And I find YouTube's discovery and playlist management to be pretty bad

**Website: [tuneconsole.com](https://tuneconsole.com)** — overview, install guide, and
[privacy policy](https://tuneconsole.com/privacy).

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

## Quickstart

1. **Start the app:**
   ```bash
   uv sync
   uv run yt-playlist        # serves http://127.0.0.1:8765
   ```
2. **Install the browser extension** (Chrome/Chromium/Edge): go to `chrome://extensions`, turn on
   **Developer mode**, click **Load unpacked**, and select the `extension/` directory. It connects
   to the app automatically, there is nothing to paste (see `extension/README.md`).
3. **Open `https://music.youtube.com` signed in**. The app pairs with the extension and starts syncing your library in the background.

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


## Architecture

### Database upgrades and catalog identity

Database upgrades run before repositories or background workers start. The ordered
registry is `src/yt_playlist/core/migrations/registry.py`; add a new migration there
and keep released versions unchanged. A migration uses explicit SQL on the supplied
connection. The runner owns its transaction, version stamp and rollback; migration
code must never commit, use `executescript`, or call repository write methods.

Before upgrading an existing database, TuneConsole creates and integrity-checks a
SQLite snapshot in `backups/` beside that database. Snapshot filenames identify
the source and target schema versions. Fresh installations and reopening a current
database do not create migration backups. Startup reports failed versions without
starting the application against a partly upgraded schema.

To recover, stop TuneConsole, preserve the failed database together with any `-wal`,
`-shm` or journal files, and copy the verified backup to the database location.
Remove the stopped database's old sidecar files from that location before reopening.
Use an application release compatible with the backup's schema. Recovery is manual;
TuneConsole never automatically downgrades or restores over your database.

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
Derived vectors are rebuilt in identity version 2, and incompatible model rollback
artifacts remain available for inspection rather than being loaded into that space.

### The stack

TuneConsole is a single local process.

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
outcome. Historical impressions without coverage/provenance remain unknown. Reports expose sample
sizes per slice, mark fewer than 100 impressions as collecting evidence, and never infer a comparative
verdict from observational data. Recall/projection metrics remain component diagnostics.

The Taste model health panel shows response and coverage summaries. `GET /taste/scorecard` returns
the full scorecard (optional Unix-second `start` and `end`, at most 90 days);
`GET /taste/served/{id}` returns an impression and its stored policy for inspection.
