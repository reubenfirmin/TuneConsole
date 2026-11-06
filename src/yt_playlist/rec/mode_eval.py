"""Taste-mode choice evidence for the chart and Home sampler.

The chart shows offers and successful playlist creations with bounded selection uncertainty.
Serving uses compiled, windowed context-response evidence when available, with legacy choice
counts only before the first compilation. Reads only; writes nothing.
"""

import math

import numpy as np
from yt_playlist.rec import choice_bandit, rec_params


def choice_posterior(picks, offers):
    """Parameters of the selection distribution used by the Home theme sampler."""
    return 1 + picks, 1 + max(0, offers - picks)


def _choice_contrast(rows, strongest):
    """One supported contrast, not predicted menu shares or absolute increases.

    Compare the best-supported choice with the most-offered group whose middle-80% interval
    is entirely below it. Untested groups remain exploratory. Identical labels are separate
    model groups and must not be merged or presented as opposing preferences.
    """
    if not strongest:
        return None
    leader = strongest[0]
    for row in sorted(rows, key=lambda r: (-r["offered"], r["mode_id"])):
        if (row["has_evidence"] and row["high"] < leader["low"]
                and row["label"].casefold() != leader["label"].casefold()):
            return {"favored": [leader], "less_supported": [row]}
    return None


def choice_evidence(store, now=None):
    """Expose legacy creation/impression evidence without calling saves confirmed listening.

    Picks are successful playlist creations, not unique clicks per offer. Preserve both raw
    counts, including repeat selections, and use the sampler's bounded posterior rather than
    presenting their ratio as a click-through rate. Intervals use a fixed simulation so the
    read-only chart stays stable between visits and needs no statistics dependency.
    """
    since = max(0, now - rec_params.get_param(store, "bandit_window_d") * 86400) if now is not None else None
    until = math.nextafter(now, math.inf) if now is not None else None
    offered = store.modes.impression_counts(since, before=until)
    picked = store.modes.pick_counts(since, before=until)
    rows = []
    for color, mode in enumerate(store.modes.list_modes(active_only=True)):
        mid = mode["mode_id"]
        offers, picks = offered.get(mid, 0), picked.get(mid, 0)
        # The sampler ignores orphan picks until an impression exists for that mode.
        alpha, beta = choice_posterior(picks if mid in offered else 0, offers)
        rng = np.random.default_rng(mid)
        low, high = np.quantile(rng.beta(alpha, beta, 16384), [0.1, 0.9])
        rows.append({"mode_id": mid, "label": mode["label"], "color_idx": color,
                     "offered": offers, "picked": picks, "has_evidence": mid in offered,
                     "alpha": alpha, "beta": beta,
                     "estimate": alpha / (alpha + beta), "low": float(low), "high": float(high)})
    strongest = sorted((r for r in rows if r["picked"] and r["offered"]),
                       key=lambda r: (-r["low"], r["mode_id"]))[:3]
    return {"rows": rows, "strongest": strongest, "contrast": _choice_contrast(rows, strongest),
            "window_days": rec_params.get_param(store, "bandit_window_d") if now is not None else None,
            "offered": sum(r["offered"] for r in rows),
            "picked": sum(r["picked"] for r in rows)}


def mode_bandit_stats(store, *, before=None, now=None) -> dict:
    """{mode_id: (positive mass, total mass)}, pooled across framings.

    Compiled responses use their declared cutoff, generation and policy. Legacy creation counts
    are a compatibility fallback before the first compilation. `before` freezes legacy evidence
    at a menu's first display so that menu only influences a later rotation.
    """
    # Explicit dates select the serving window. Undated callers retain the legacy
    # all-time diagnostic; serving always supplies its decision timestamp.
    end = before if before is not None else now
    if end is not None:
        compiled = choice_bandit.stats(store, end)
        if compiled is not None:
            return compiled
    since = max(0, end - rec_params.get_param(store, "bandit_window_d") * 86400) if end is not None else None
    until = before if before is not None else math.nextafter(now, math.inf) if now is not None else None
    offered = store.modes.impression_counts(since, before=until)
    picked = store.modes.pick_counts(since, before=until)
    return {mid: (picked.get(mid, 0), n) for mid, n in offered.items()}
