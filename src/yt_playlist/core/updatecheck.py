"""Backend update check: is a newer release than the running version available?

The running version is baked in by hatch-vcs. Release metadata comes through the GitHub
IAO. This owner schedules checks, reads and sanitizes the running version, detects how the
backend was installed, compares versions and builds the update payload the Home page renders.
"""
import importlib.metadata
import json
import os
import sys

from yt_playlist.integrations.base import AccessState
from yt_playlist.integrations.github import GitHubReleasesIAO
from yt_playlist.controllers.updates import UpdateChecks
from yt_playlist.core.update_rules import CHECK_INTERVAL_S, out_of_date as _out_of_date, version_tuple as _ver_tuple

PACKAGE = "yt-playlist"

_CHECK_INTERVAL_S = CHECK_INTERVAL_S
_github = GitHubReleasesIAO()


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
    """Project the IAO's public release metadata to the existing version format."""
    data = _github.latest_release()
    if data is None:
        return None
    tag = (data.get("tag_name") or "").lstrip("v").strip()
    return tag or None


def schedule_first_check(store, now):
    """Give a fresh install a full interval before its first automatic release request.

    Persist the deadline so restarting during onboarding does not reset the delay.
    Existing installations with a checked or cached release keep their schedule.
    """
    checks(store.settings, AccessState.for_store(store)).schedule_first(now)


def check_latest(store, now, *, interval_s=_CHECK_INTERVAL_S):
    """At most once per interval_s, refresh the cached latest release. Always stamps the check time
    (so a persistent failure retries at most once per interval). Access errors are silent;
    explicit work cancellation propagates without stamping a completed check."""
    checks(store.settings, AccessState.for_store(store)).check(now, interval_s=interval_s)


def checks(settings, access):
    """Bind host metadata and release I/O to the detached command owner."""
    def latest():
        with _github.context(access):
            return _fetch_latest_release()
    return UpdateChecks(settings, latest, current_version=current_version, install_kind=install_kind)


def install_kind() -> str:
    """How this backend was installed: 'dev' (source/editable checkout), 'flatpak' (sandbox env),
    'macos' (frozen .app), 'container' (image env), else an unrecognized 'release'.
    'dev' is checked first: checkout code must not get packaged-release advice."""
    if is_dev_install():
        return "dev"
    if os.environ.get("YT_PLAYLIST_CONTAINER") == "1":
        return "container"
    if os.environ.get("FLATPAK_ID"):
        return "flatpak"
    if getattr(sys, "frozen", False):
        return "macos"
    return "release"


def update_nudge(store):
    """Nag payload dict when the backend is behind the latest seen release and the user has not
    dismissed that exact version, else None. Keys: current, latest, kind. `kind` is a neutral
    token; the update command/label/link for it is presentation (see web/update_labels.py).

    A source/editable checkout (is_dev_install) is never nagged: it runs HEAD code, its reported
    version is a stale hatch-vcs snapshot, and it is not managed by any package manager, so 'you are
    behind, reinstall a release' would be both wrong and unactionable."""
    return checks(store.settings, AccessState.for_store(store)).offer()
