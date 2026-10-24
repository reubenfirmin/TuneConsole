# Asset ownership and cascade

Templates load `tokens.css` first, then `app.css` for the document frame, navigation,
search, page headings and responsive rail. `components.css` owns reusable layout,
cards, buttons, forms, progress, notices and modal surfaces. `tables.css` supplies
sorting and HTMX row primitives. `song-actions.css` and `genre-controls.css` own the
shared song menus, dialogs and metadata controls; they load with the document because
any page can open them through search or HTMX. `visualization-tooltips.css` and its
script own the delegated pointer and keyboard readouts.
`playback.css` owns the shared player and its mobile strip and desktop sidebar dock; it
loads on every normal page, independently of Home's assets.
Feature styles follow; `artwork.css` supplies the final shared artwork shapes and template
`page_styles` adds a page's own controls. There is no global override stylesheet.
Use semantic tokens for colors and shared control sizes. A page may use local display sizes.

`base.html` selects assets from the request path. `home.js`/`home.css` own Home's page,
arrival and sync status. `home-board.js`/`home-board.css` own board focus, mix regeneration,
animation and responsive geometry. `home-tuning.js`/`home-tuning.css` own temporary genre,
vibe and fingerprint controls, their expiry and dialogs. `home-discovery.js` owns visible
tile/story measurements; `home-discovery.css` owns those tiles and stories, including their
shared library detail uses. Board transitions dispatch `home:mix-settled` so tuning can
refresh its displayed genres without calling another owner's implementation.
`generated-mixes.css` and `mix-feedback.css` also load on their Generated, Taste and Road
Trip consumers. `taste-primitives.css` supplies Taste's sliders, preview and trace layouts
before its panel styles. Range and HTMX indicator primitives live in `components.css`.
Road Trip and Setup own their script files.
Radio's session controls live in `radio.js`; `radio-artwork.js` owns contour and junction
geometry, history/queue measurements, motion listeners and saved drawings. The session
adapter supplies the documented `RadioArtworkView` (drawing identity, visibility, motion,
preview slot and direction count). Artwork does not read session payloads or issue requests.
The adapter composes artwork property descriptors to preserve reactive geometry getters.
`library-controls.css` owns playlist selection, editable titles, mastheads and track
table geometry with their responsive rules. It precedes the library page styles.
Library mastheads, selection and view controls use `library.css`. `library-tables.css` owns
the shared library card surfaces, row artwork sizes and entity table columns, including
Home's radio table. Detail routes additionally load `library-entities.css` for artist,
album and playlist artwork mastheads and actions. These modules follow the shared table
primitives and precede `artwork.css`, which keeps final shared artwork shapes consistent.
Keep each component's responsive rules with its owner. Charts adds `charts.css`, and
operational pages use `workflows.css`; Merge adds `merge.css`. The reusable genre combo has
its own stylesheet.
Taste loads `visualizations.css` before its panel owners: `taste-controls.css` for settings
and feedback, `taste-learning.css` for recent evidence, `taste-evidence.css` for overview and
generation explanations, `taste-profile.css` for the profile and source influence, and
`taste-health.css` for prediction checks and automatic selection. Load all five with the
initial document because HTMX panels do not replace the head. Visualization primitives
contain SVG roses, bar/chain readings and responsive grids; feature layouts stay with panels.
Enrichment uses `enrichment.css`; `enrichment-conflicts.css` also loads on library detail
routes for the shared resolver. Discovery adds `discovery.css` after its workflow controls.
Road Trip uses `road-trip.css`; Home's featured subject uses `home-observation.css`.
The former analytics bundle's Trends instrument charts, insight cards, review calendar and
library-health layouts have no live template or script consumers and were removed. The old
Wikipedia card layout was also unused; its live subject rule stays with Home. Canvas
switcher rules now live in `clusters.css`, where the saved-canvas controls are rendered.
The graph vendor scripts, renderer and graph CSS load only in Clusters. Generating and the
story reel are standalone templates with their own styles and no application feature bundle.

Clusters loads `cluster-state.js`, `cluster-storage.js` and `cluster-renderer.js` before
the classic `clusterCanvas` factory in `cluster.js`, all before deferred Alpine.
`ClusterGraph` computes topology, ring placement, pruning, genre filtering and branch
separation from explicit graph inputs. `ClusterCanvases` owns the v1 snapshot schema,
legacy migration and multi-canvas storage. The renderer receives a view containing raw
simulation nodes, topology, visual controls and overlay targets; it owns drawing, picking,
image/palette caches and force-graph setup. The adapter commits graph results onto surviving
simulation objects, owns UI events and requests, and retains every revision/token check
before publishing an async result. Camera movement remains outside the curation signature.

Scripts remain classic globals because server-rendered Alpine expressions call their factories.
`app.js` is the shell entry: topbar measurement, auth banner, omni-search and HTMX
confirmation/error handling. `player-card.js` consumes the single `playbackStatus`
owner; `playback-controls.js` owns playback links, keyboard shortcuts and media controls.
`song-actions.js` and `genre-dialog.js` remain shared for fragment-mounted song dialogs.
Only library/table pages load `table-controls.js`; playlist and Move pages load
`library-playlists.js`. Detail pages load `enrichment-panel.js`, and track surfaces
load `track-tables.js` for suggestion staging, current-row highlighting and metadata
refresh. `playlist-generation.js` loads only on Home and Road Trip. Each owner contains
the implementation, with no shell forwarding wrappers or secondary playback poller.
Load feature factories before deferred Alpine. Navigation currently performs full page loads;
HTMX swaps fragments within their owning page. Use `htmx.swap`/`htmx.process` for injected
controls so HTMX and Alpine initialize replacements. Alpine components close SSE connections,
clear timers, and destroy Sortable instances in `destroy()`. Home's delegated listeners are
registered once per document; fragment cleanup releases observers and animation state.
Preserve rejected HTMX requests when processing error toasts, so optimistic controls can roll back.
