"""The GENERATED_GROUP quarantine (repos/base.py) is a real invariant that the Trends page violated for
its entire life, because the rule lived only in a docstring and nothing enforced it. This test makes it
fail loudly instead.

A module that derives TRACK-LEVEL taste signal from play history must not read raw plays without
excluding plays sourced from playlists this app generated. Otherwise the engine feeds on its own
suggestions: TuneConsole recommends a track, you play it from the generated playlist, and that play
comes back as evidence that you like it.

MODE-LEVEL selection signal is exempt by design. Each generated playlist is built from one taste mode,
so choosing to play it is weak evidence for that MODE at the top of the model stack. It says nothing
about the tracks. rec_mode_picks / rec_mode_impressions / ledger_mode_plays live in that tier and must
keep their generated-playlist provenance.
"""
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "yt_playlist"

# Modules that derive track-level taste signal from play history. Adding one here without applying the
# quarantine is the bug this test exists to catch.
TASTE_SIGNAL_MODULES = [
    "rec/trend_rollups.py",
]

# Reads of raw play data that bypass the quarantine unless the module also applies an exclusion.
RAW_PLAY_READS = ("play_day_counts(", "month_track_plays(", "exclude_generated=False")

# The sanctioned ways to read plays with the quarantine applied.
QUARANTINE_MARKERS = ("day_counts_source(", "ledger_day_counts()", "ledger_track_plays(",
                      "ledger_artist_plays(", "GENERATED_GROUP")


@pytest.mark.parametrize("rel", TASTE_SIGNAL_MODULES)
def test_taste_signal_modules_exclude_generated(rel):
    src = (SRC / rel).read_text()
    raw = [r for r in RAW_PLAY_READS if r in src]
    if not raw:
        return                                  # reads no raw plays at all: nothing to quarantine
    assert any(m in src for m in QUARANTINE_MARKERS), (
        f"{rel} reads plays via {raw} but never applies the GENERATED_GROUP quarantine.\n"
        f"See repos/base.py: generated-playlist plays must not feed track-level taste signal, or the "
        f"engine feeds on its own suggestions.\n"
        f"If this module computes MODE-level signal only, remove it from TASTE_SIGNAL_MODULES and say "
        f"why in a comment.")


def test_trend_rollups_repetition_series_carries_the_exclusion():
    """The module-level guard above is satisfied by the mere PRESENCE of `day_counts_source(`, so it
    cannot see that detect_insights has a SECOND, separate play read feeding the repetition detector.
    That read (play_events_since) leaked generated-playlist plays for its whole life. Pin the specific
    call site: an unguarded `play_events_since(0)` must never come back."""
    src = (SRC / "rec" / "trend_rollups.py").read_text()
    assert "play_events_since(0)" not in src, (
        "detect_insights reads play_events_since(0) with no exclusion, so generated-playlist plays feed "
        "the repetition detector (the day_counts quarantine does NOT cover this separate read).\n"
        "Pass exclude_list_ids=store.trends.generated_ytm_ids().")


def test_mode_level_signal_documents_why_it_is_exempt():
    """rec_mode_picks intentionally retains generated-playlist provenance. Pin the rationale in place so
    a future cleanup does not "fix" the exemption by deleting the signal it exists to capture."""
    src = (SRC / "repos" / "modes.py").read_text().lower()
    assert "mode-level" in src or "weak signal" in src, (
        "repos/modes.py must explain WHY generated-playlist picks are kept, or someone will quarantine "
        "them too and silently destroy the mode selection signal.")
