"""Backend update check: is a newer release than the running version available?

The running version is baked in by hatch-vcs. The latest release comes from the GitHub
releases API (see check_latest, Task 3). This module holds the pure logic: reading and
sanitizing the running version, detecting how the backend was installed (to give the right
update command), comparing versions, and building the nag payload the Home page renders.
"""
import importlib.metadata
import json
import os
import sys
import urllib.request

PACKAGE = "yt-playlist"

_RELEASES_URL = "https://api.github.com/repos/reubenfirmin/TuneConsole/releases/latest"
_HTTP_TIMEOUT_S = 8
_CHECK_INTERVAL_S = 86400          # once a day; well under GitHub's 60 req/hr unauth limit
_USER_AGENT = "yt-playlist-updatecheck"

# (command-or-label, optional link) per install kind. Flatpak is a copy-paste command; packaged macOS
# and otherwise-unrecognized release installs go to the release page. TuneConsole does not offer or
# document pip-based installation, so the update card must not invent a pip upgrade path. There is
# deliberately no "dev" entry: a source/editable checkout is never nagged (see update_nudge), so it
# never needs an update command -- you update it with git, not a package manager.
_INSTRUCTIONS = {
    "flatpak": ("flatpak update --user com.tuneconsole.TuneConsole", None),
    "macos": ("Get the latest release", "https://github.com/reubenfirmin/TuneConsole/releases/latest"),
    "release": ("Get the latest release", "https://github.com/reubenfirmin/TuneConsole/releases/latest"),
}


def _raw_version() -> str:
    """The raw installed version string (indirection so tests can substitute one)."""
    return importlib.metadata.version(PACKAGE)


def current_version() -> str:
    """Running version sanitized to base X.Y.Z (drops a .devN and/or +g<hash> local suffix)."""
    return _raw_version().split(".dev")[0].split("+")[0]


def _is_editable_install() -> bool:
    """True when the running dist is an editable/local install (pip/uv `-e`, i.e. run from a checkout).
    Reads the PEP 610 direct_url.json metadata. Fail-safe to False on any metadata hiccup."""
    try:
        raw = importlib.metadata.distribution(PACKAGE).read_text("direct_url.json")
        if not raw:
            return False
        return bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except Exception:                       # noqa: BLE001 - missing/odd metadata must not surface
        return False


def is_dev_install() -> bool:
    """True for a source/checkout build rather than a published release. Two signals:
    a PEP 440 `.dev` or `+local` segment in the baked version (hatch-vcs stamps these on any commit
    that is not exactly a tag), or an editable install. Such a build runs HEAD code straight from a
    checkout, so it is never meaningfully 'behind' a release and must never be nagged to reinstall
    (its sanitized version is a stale snapshot that can read as older than, newer than, or divergent
    from any release)."""
    raw = _raw_version()
    if ".dev" in raw or "+" in raw:
        return True
    return _is_editable_install()


def _fetch_latest_release():
    """Single network seam (tests monkeypatch this). GET the releases API and return the latest
    tag as base X.Y.Z, or None. Raises on network/HTTP errors; check_latest swallows them."""
    req = urllib.request.Request(
        _RELEASES_URL,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
        data = json.load(resp)
    tag = (data.get("tag_name") or "").lstrip("v").strip()
    return tag or None


def check_latest(store, now, *, interval_s=_CHECK_INTERVAL_S):
    """At most once per interval_s, refresh the cached latest release. Always stamps the check time
    (so a persistent failure retries at most daily, never hammering GitHub). Fail-silent."""
    last = store.get_setting("latest_version_checked_at")
    if last is not None:
        try:
            if now - float(last) < interval_s:
                return
        except (TypeError, ValueError):
            pass
    try:
        latest = _fetch_latest_release()
    except Exception:                       # noqa: BLE001 - offline/HTTP/parse must not surface
        latest = None
    store.set_setting("latest_version_checked_at", str(now))
    if latest:
        store.set_setting("latest_version_seen", latest)


def install_kind() -> str:
    """How this backend was installed: 'dev' (source/editable checkout), 'flatpak' (sandbox env),
    'macos' (frozen .app), else an unrecognized 'release'. 'dev' is checked first: a checkout inside a flatpak
    or frozen bundle is still checkout code, and packaged-release advice would be wrong."""
    if is_dev_install():
        return "dev"
    if os.environ.get("FLATPAK_ID"):
        return "flatpak"
    if getattr(sys, "frozen", False):
        return "macos"
    return "release"


def update_instruction(kind: str):
    """(command-or-label, optional-link) describing how to update this install kind."""
    return _INSTRUCTIONS.get(kind, _INSTRUCTIONS["release"])


def _ver_tuple(v):
    """Base version 'X.Y.Z' -> a 3-int tuple for ordering (pads/truncates to 3 parts)."""
    parts = [int(p) for p in str(v).split(".")]
    return tuple((parts + [0, 0, 0])[:3])


def _out_of_date(current: str, latest: str) -> bool:
    """True iff current < latest. Both are sanitized base versions (X.Y.Z). Non-numeric -> not out of date."""
    try:
        return _ver_tuple(current) < _ver_tuple(latest)
    except (ValueError, TypeError):
        return False


def update_nudge(store):
    """Nag payload dict when the backend is behind the latest seen release and the user has not
    dismissed that exact version, else None. Keys: current, latest, kind, command, link.

    A source/editable checkout (is_dev_install) is never nagged: it runs HEAD code, its reported
    version is a stale hatch-vcs snapshot, and it is not managed by any package manager, so 'you are
    behind, reinstall a release' would be both wrong and unactionable."""
    if is_dev_install():
        return None
    latest = store.get_setting("latest_version_seen")
    if not latest:
        return None
    current = current_version()
    if not _out_of_date(current, latest):
        return None
    if store.get_setting("backend_update_dismissed_version") == latest:
        return None
    kind = install_kind()
    command, link = update_instruction(kind)
    return {"current": current, "latest": latest, "kind": kind, "command": command, "link": link}
