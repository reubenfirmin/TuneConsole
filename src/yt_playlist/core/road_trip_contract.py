"""Persisted Road Trip recipe units and supported user controls."""

DEFAULT_FAMILIARITY_PCT = 50
MIN_TARGET_MINUTES = 15
MAX_TARGET_MINUTES = 12 * 60


def validate_recipe_values(own_pct, familiarity_pct, target_minutes, now):
    for name, value in (('own_pct', own_pct), ('familiarity_pct', familiarity_pct)):
        if type(value) is not int or not 0 <= value <= 100:
            raise ValueError(f'{name} must be an integer percentage from 0 through 100')
    if type(target_minutes) is not int or not MIN_TARGET_MINUTES <= target_minutes <= MAX_TARGET_MINUTES:
        raise ValueError(f'target_minutes must be an integer from {MIN_TARGET_MINUTES} through '
                         f'{MAX_TARGET_MINUTES} minutes')
    if type(now) not in (int, float) or not -1.7976931348623157e308 <= now <= 1.7976931348623157e308:
        raise ValueError('Recipe time must be finite epoch seconds')
