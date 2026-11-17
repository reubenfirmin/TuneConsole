"""Capture and commands for listening-format defaults."""
from yt_playlist.rec.home_format_rules import *


def default_format(store):
    override = store.settings.get_setting('home_format')
    if override in PANELS:
        return select_format(override=override)
    last = store.settings.get_setting('home_last_panel')
    if override == 'both':
        return select_format(override=override, last=last)
    return select_format(override=override, last=last, usage=store.rec_proposals.choice_usage(),
                         stated=store.settings.get_setting('onboard_listening_format'))


def default_panel(store, *, albums_ready=False, mixes_ready=False):
    return select_panel(default_format(store), override=store.settings.get_setting('home_format'),
                        albums_ready=albums_ready, mixes_ready=mixes_ready)


def resolve_default_panel(store, growth):
    from yt_playlist.rec.growth_access import has_access
    panel = default_panel(store, albums_ready=growth['ready_formats']['albums'],
                          mixes_ready=growth['ready_formats']['mixes'])
    preferred = PANELS[default_format(store)]
    if has_access(growth, preferred):
        panel = preferred
    return panel if has_access(growth, panel) else 'unlocks'


def choose(store, choice, *, ready_formats=None, now=None, radio=None):
    if ready_formats is None:
        from yt_playlist.rec import account_growth
        ready_formats = account_growth.view(store, now, radio=radio.bootstrap())['ready_formats']
    validate_choice(choice, ready_formats)
    store.settings.set_setting('home_format', choice)
