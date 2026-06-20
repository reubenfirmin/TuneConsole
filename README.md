# yt-playlist

Local web tool to dedupe, merge, and prune YouTube Music playlists across YouTube brand identities.

## Quickstart
```bash
uv sync
uv run yt-playlist        # serves http://127.0.0.1:8765
```
See `docs/superpowers/specs/` for the design.

## Setup (in the browser)

Just run it — no config files needed up front:
```bash
uv run yt-playlist            # serves http://127.0.0.1:8765
```
Until it's configured, every page redirects to **`/setup`**, a guided wizard that:

1. Walks you through capturing a signed-in `music.youtube.com` request — DevTools → Network →
   a `browse` request → **Copy as cURL** — and pasting it in. A **Check sign-in** button
   live-verifies it and shows who you're signed in as.
2. Lets you add one or more **identities** (label + optional `brand_account_id`). **Exactly one
   must be the master** — the consolidation target that cross-identity merges move playlists into.
   The `brand_account_id` is the brand-account **user id** (from
   `myaccount.google.com/brandaccounts` → pick the account → the `…/b/<id>/` in the URL), *not*
   the `UC…` channel id. Leave it blank for your main account.

On save it writes `~/.config/yt-playlist/browser.json` and `config.toml` for you and drops you on
the dashboard. You can revisit **Setup** in the nav anytime to add identities or refresh auth.

`yt-playlist --help` lists options (`--host`, `--port`); `YT_PLAYLIST_HOME` overrides where config
and data live.

### Manual config (optional)

Prefer to write `~/.config/yt-playlist/config.toml` yourself? The wizard just produces this:
```toml
[[identity]]
label = "main"
credential_ref = "browser.json"
is_master = true

[[identity]]
label = "brand"
credential_ref = "browser.json"
brand_account_id = "1234567890..."   # brand-account user id from myaccount.google.com/b/<id>/
```
Capture the credential with `uv run ytmusicapi browser` and move `browser.json` next to it.
