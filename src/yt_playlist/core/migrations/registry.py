"""Applied numbered history, retained as data without historical converters."""
from .runner import Migration
from ..destination_schema import definition

MIGRATIONS = tuple(Migration(version, name) for version, name in definition()['numbered_history'])
