"""Intensity-axis journeys use the same prepared dimension as Chill to Full on.

Unknown intensity orders neutrally. Tempo, neighboring candidates and genre
family averages cannot change an unchanged recording's intensity.
"""
from yt_playlist.rec import musical_character


def arc_energies(keys, profiles):
    return {key: musical_character.intensity({'musical': profiles.get(key)}) for key in keys}
