"""Readiness capacity rules over feature-owned output and supplied configuration."""
import math


def prepared_taste(context, content_generation, collaborative_inputs):
    policy = context.get('policy') or {}
    if (policy.get('content_generation') != content_generation
            or policy.get('collaborative_inputs') != collaborative_inputs):
        return False
    saved = (context.get('layers') or {}).get('saved') or {}
    return any((value or {}).get('titles') for value in saved.values())


def foundation_ready(active_modes, contexts, content_generation, collaborative_inputs, tracks, capacity):
    taste = bool(active_modes) or any(prepared_taste(context, content_generation, collaborative_inputs) for context in contexts)
    ranked = {track['key'] for track in tracks
        if type(track.get('score')) in (int, float) and math.isfinite(track['score'])}
    return bool(taste and len(ranked) >= capacity)


def albums_ready(menu, capacity):
    return len(menu) >= capacity
