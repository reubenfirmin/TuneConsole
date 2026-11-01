"""Stable artifact names for large, explicitly recorded source combinations."""
import hashlib


def compact_slug(value):
    if len(value.encode()) <= 220:
        return value
    prefix = value.encode()[:96].decode(errors='ignore')
    return prefix + '__' + hashlib.sha256(value.encode()).hexdigest()[:24]
