"""Presentation labels for the backend update nag card, keyed by the neutral install kind
`core.updatecheck.install_kind` returns.

Flatpak and macOS have in-app controls; otherwise-unrecognized release installs go to the
release page. TuneConsole does not offer or document pip-based installation, so the update card
must not invent a pip upgrade path. There is deliberately no "dev" entry: a source/editable
checkout is never nagged (see updatecheck.update_nudge), so it never needs an update command --
you update it with git, not a package manager.
"""
_INSTRUCTIONS = {
    "flatpak": ("Update now", None),
    "macos": ("Update now", None),
    "container": ("Update your container", "https://tuneconsole.com/#container-updates"),
    "release": ("Get the latest release", "https://github.com/reubenfirmin/TuneConsole/releases/latest"),
}


def update_instruction(kind: str):
    """(command-or-label, optional-link) describing how to update this install kind."""
    return _INSTRUCTIONS.get(kind, _INSTRUCTIONS["release"])
