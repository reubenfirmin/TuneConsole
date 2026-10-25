"""One explicit calendar for prepared analytics and editorial editions."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from yt_playlist.core.listening import machine_timezone


def timezone_name(value=None):
    name = value or machine_timezone()
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return 'UTC'


def day(at, name='UTC'):
    return datetime.fromtimestamp(at, ZoneInfo(name)).date().isoformat()


def edition_day(at, name):
    return (datetime.fromtimestamp(at, ZoneInfo(name)) - timedelta(hours=6)).date().isoformat()


def midnight(at, name):
    return datetime.fromtimestamp(at, ZoneInfo(name)).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
