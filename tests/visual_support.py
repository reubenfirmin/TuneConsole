"""Optional diagnostic images; assertions that inspect pixels still capture directly."""
import os
from pathlib import Path


def capture(target, **kwargs):
    if os.getenv('TUNECONSOLE_VISUAL_REVIEW') == '1':
        if kwargs.get('path'):
            Path(kwargs['path']).parent.mkdir(parents=True, exist_ok=True)
        return target.screenshot(**kwargs)
