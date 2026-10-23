# Asset ownership and cascade

Templates load `tokens.css` first, then `app.css` for the shell and shared controls.
`playback.css` owns the shared player and its mobile strip and desktop sidebar dock; it
loads on every normal page, independently of Home's assets.
Feature styles follow; `artwork.css` supplies the final shared artwork shapes and template
`page_styles` adds a page's own controls. There is no global override stylesheet.
Use semantic tokens for colors and shared control sizes. A page may use local display sizes.

`base.html` selects assets from the request path. Home owns its board, tuning controls and
discovery observation in `home.js`/`home.css`; Road Trip and Setup own their script files.
Radio's session controls live in `radio.js`; `radio-artwork.js` owns its contour geometry,
session-specific motion and saved drawing state.
Library indexes/details use `library.css`, Charts adds `charts.css`, and operational pages
use `workflows.css`; Merge adds `merge.css`. The reusable genre combo has its own stylesheet.
The graph vendor scripts, renderer and graph CSS load only in Clusters. Generating and the
story reel are standalone templates with their own styles and no application feature bundle.

Scripts remain classic globals because server-rendered Alpine expressions call their factories.
Load feature factories before deferred Alpine. Navigation currently performs full page loads;
HTMX swaps fragments within their owning page. Use `htmx.swap`/`htmx.process` for injected
controls so HTMX and Alpine initialize replacements. Alpine components close SSE connections,
clear timers, and destroy Sortable instances in `destroy()`. Home's delegated listeners are
registered once per document; fragment cleanup releases observers and animation state.
Preserve rejected HTMX requests when processing error toasts, so optimistic controls can roll back.
