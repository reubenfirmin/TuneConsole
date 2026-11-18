"""Graduation thresholds and exposure arithmetic over supplied facts."""
import time


def graduation_factor(score, threshold, *, up_factor, down_factor):
    if abs(score) < threshold:
        return None
    return up_factor if score > 0 else down_factor


def mood_contributions(keys, facets, signed):
    if signed <= 0:
        return {}
    n = len(set(keys)) or 1
    return {axis: signed * len(axis_keys) / n for axis, axis_keys in facets.items()}


def migrated_lean(value, old_permanent, new_permanent):
    ratio = new_permanent / old_permanent if old_permanent > 0 else 1.
    return value / ratio


def utc_day(now):
    return time.strftime('%Y-%m-%d', time.gmtime(now))
