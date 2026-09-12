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
- **Library browsing**: dedicated Artists, Albums, Charts, and Genres views.
- **Clusters**: a fun visual approach to building playlists.
- **Road Trip**: build a playlist for a road trip that combines your and passenger's tastes.
- **Recommendations & discovery**: surfaces new artists, rediscoveries, and a personalized
  "for you" feed driven by the taste model.
- **Metadata enrichment**: uses Last.fm, Deezer, Discogs, MusicBrainz and AcousticBrainz.

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


## Architecture

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

### The model

The recommender is local, CPU-only, and trained on your own library and listening history. Its
implementation lives in `src/yt_playlist/rec/` and has four main parts:

- **Representations.** `embed.py` maintains a collaborative track space from co-occurrence baskets
  (playlists, albums, artists, listening sessions, genre families, and decades). The default builder
  is PPMI plus truncated SVD; Auto-tune can select item2vec instead. A separate content space encodes
  genre, era, musical key, and enriched audio features. It supports taste modes, clustering, and
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
