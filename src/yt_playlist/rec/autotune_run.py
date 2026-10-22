"""Read historical manual Auto-tune reports. New comparisons use model_selection."""
import json

RESULT_SETTING = "rec_autotune_result"


def last_result(store) -> dict | None:
    raw = store.settings.get_setting(RESULT_SETTING)
    if not raw:
        return None
    try:
        result = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return result if isinstance(result, dict) else None
