"""Optional diagnostic images; assertions that inspect pixels still capture directly."""
import os


def capture(target, **kwargs):
    if os.getenv('TUNECONSOLE_VISUAL_REVIEW') == '1':
        return target.screenshot(**kwargs)
