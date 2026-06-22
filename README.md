# TuneConsole

A local web console for power-managing your YouTube Music library across multiple YouTube brand
identities. Dedupe, merge, prune, organize, and grow it, all from one place.

## Features

- **Cross-identity consolidation**: sign in to several YouTube brand accounts and merge or move
  playlists into one master library.
- **Dedupe, merge & prune**: find duplicate and overlapping playlists, merge them, and delete
  empties. Every destructive action is undoable.
- **Omnisearch**: instant search across playlists, artists, albums, and tracks in your whole library.
- **Library browsing**: dedicated Artists, Albums, Charts, and Genres views.
- **Clusters**: an interactive force-directed graph of how your library hangs together.
- **Taste model**: a tunable model of your taste, with its own control panel.
- **Recommendations & discovery**: surfaces new artists, rediscoveries, and a personalized
  "for you" feed driven by the taste model.
- **Generative playlists**: auto-build playlists from your taste; unplayed ones are
  garbage-collected after a grace window.
- **Play-history sync**: pulls play counts to keep recommendations and cleanup current.
- **Guided browser setup**: capture auth and add identities through a wizard; no config files
  needed up front.

## Quickstart
```bash
uv sync
uv run yt-playlist        # serves http://127.0.0.1:8765
```
Design specs live in `docs/superpowers/specs/`.

## Architecture

### The stack

TuneConsole is a single local process with no build step and no SPA.

- **Python / FastAPI**: an ASGI app served by uvicorn (`--reload` supported). Routes return
  server-rendered HTML, not JSON.
- **Jinja2**: every page and partial is a server-rendered template.
- **HTMX**: interactions are HTML over the wire. Buttons and inputs issue requests that swap in
  server-rendered fragments, so the server stays the source of truth and there is no client-side
  state to keep in sync.
- **Alpine.js**: the thin layer of client reactivity HTMX does not cover, such as menus, the
  omnisearch box, and optimistic toggles. Sortable.js handles drag-to-reorder and d3-force draws
  the Clusters graph.
- **SQLite**: the whole library, play history, and model state live in one local SQLite file.
  `store.py` composes per-domain DAOs (the `repos/` package) behind a single connection.
- **ytmusicapi**: the only outbound dependency, used to talk to YouTube Music. Frontend libraries
  are vendored locally, so nothing is fetched from a CDN at runtime.

### The model

Recommendations run entirely on your own library, CPU-only, with no GPU and no external pretrained
models. Two models work in tandem: a **long-term** model of your settled taste, rebuilt from your
whole library, and a **transient** model that nudges it toward what you are into right now. The
long-term model changes only when your library does; the transient one reacts to each interaction
and fades on its own. At recommendation time the transient signal tilts the long-term scores rather
than replacing them, so your baseline taste always shows through.

- **Long-term taste embedding** (`embed.py`): the stable model. A dense vector per track, built from
  PPMI co-occurrence plus truncated SVD over how tracks co-occur across your playlists, albums, and
  listening sessions. It is a latent model of *your* taste rather than the crowd's, so neighbours
  capture second-order similarity that plain co-occurrence misses. It has no decay; it is rebuilt
  only when your library changes.
- **Genre map** (`genre_map.py`): a hand-editable meta-genre family tree with family-to-family
  distances, blended into the embedding and used to measure genre diversity.
- **Transient model** (`transient.py`): the short-term counterpart to the long-term embedding. A
  fast, reactive read on recent interaction (mood feedback, recent plays, dislikes) that tilts the
  taste centroid and leans facets toward what you are into right now. It is keyed to recency of
  interaction rather than wall-clock time, and its pull relaxes back toward your long-term taste as
  a sync goes stale, so a passing mood never overwrites your settled preferences.
- **Discovery** (`discover.py`): new-artist discovery pinned to your taste. External sources
  (Last.fm) supply similarity edges, while your embedding and play-weighted taste supply the
  judgement, and each result explains which of your artists bridged to it.
- **Background worker** (`rec_worker.py`): a single thread rebuilds the taste vectors and
  materializes the heavy surfaces off the request path. Repeated syncs coalesce into one rebuild.
- **Tunable knobs** (`rec_params.py`): every result-shaping parameter is registered with a label,
  range, and default, then surfaced generically in the Taste Model control panel.

## Setup

Run it and open the dashboard. Until it is configured, every page redirects to **`/setup`**, a
guided wizard that captures your signed-in YouTube Music auth and lets you add one or more
identities (exactly one is the master that cross-identity merges consolidate into). On save it
writes your config and credentials for you and drops you on the dashboard. Revisit **Setup** in the
nav anytime to add identities or refresh auth.

Config and data live in `~/.config/yt-playlist/` (`config.toml` plus `browser.json`).
`YT_PLAYLIST_HOME` overrides that location, and `yt-playlist --help` lists options (`--host`,
`--port`).
