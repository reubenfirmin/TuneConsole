"""Compare the hidden upcoming video's timeline samples; never play it to inspect it."""
import math
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import numpy as np
from PIL import Image

from yt_playlist.egress import storyboard_url_allowed

SURFACE = 'tv_visual_checks'
VERSION = 2
TIMES = (20, 60)
LIMIT = 240
RETRY_S = 300
MAX_PIXELS = 16_000_000


def samples(storyboards, video_id, duration):
    """Choose the smallest adequate sheet and the frames covering 20s and 60s.

    Timeline previews are discretely sampled; retain their actual timestamps so
    the comparison never silently substitutes a cover thumbnail or a late frame.
    """
    if duration <= TIMES[-1]:
        return []
    spec = (storyboards.get('playerStoryboardSpecRenderer') or {}).get('spec', '')
    if not isinstance(spec, str) or len(spec) > 32000:
        return []
    base, *levels = spec.split('|')
    for level, encoded in enumerate(levels):
        try:
            width, height, count, columns, rows, interval, name, signature = encoded.split('#')
            width, height, count, columns, rows, interval = map(int, (width, height, count, columns, rows, interval))
        except (ValueError, TypeError):
            continue
        # Tiny 80x45 previews amplify JPEG/chroma noise between sprite cells.
        # The unchanged Moderat artwork crosses the motion threshold at that
        # size, while its 160x90 timeline frames correctly compare as static.
        if (min(width, height, count, columns, rows, interval) <= 0 or width < 160 or height < 90
                or width * columns * height * rows > MAX_PIXELS or interval > 10000):
            continue
        result = []
        for seconds in TIMES:
            index = math.floor(seconds * 1000 / interval)
            if index >= count:
                break
            sheet, cell = divmod(index, columns * rows)
            url = base.replace('$L', str(level)).replace('$N', name).replace('$M', str(sheet))
            parts = urlsplit(url)
            query = dict(parse_qsl(parts.query))
            query['sigh'] = signature
            url = urlunsplit(parts._replace(query=urlencode(query)))
            if not storyboard_url_allowed(url) or not parts.path.startswith(f'/sb/{video_id}/'):
                break
            result.append({'url': url, 'box': (cell % columns * width, cell // columns * height,
                                               (cell % columns + 1) * width, (cell // columns + 1) * height),
                           'at': index * interval / 1000})
        if len(result) == 2 and result[0]['at'] != result[1]['at']:
            return result
    return []


def compare(frames):
    first, second = [np.asarray(frame.resize((64, 36), Image.Resampling.BILINEAR).convert('RGB'),
                                dtype=np.float32) for frame in frames]
    # Uniform/missing placeholders are not evidence about the video's content.
    if min(float(first.std(axis=(0, 1)).mean()), float(second.std(axis=(0, 1)).mean())) < 3:
        return {'verdict': 'unknown'}
    difference = np.abs(first - second).mean(axis=2)
    mean = float(difference.mean())
    changed = float((difference > 12).mean())
    return {'verdict': 'static' if mean <= 2.5 and changed <= .02 else 'moving',
            'difference': round(mean, 4), 'changed_fraction': round(changed, 4)}


