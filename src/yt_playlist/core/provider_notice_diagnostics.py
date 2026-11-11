"""Bounded authentication and alert diagnostics shared by provider readers."""


def notice_diagnostics(response):
    """Identify known provider notices without publishing text or treating notices as absence."""
    context = response.get('responseContext')
    context = context.get('mainAppWebResponseContext') if isinstance(context, dict) else None
    logged_out = context.get('loggedOut') if isinstance(context, dict) else None
    result = {'authentication': ('signed_out' if logged_out is True else
                                 'signed_in' if logged_out is False else 'unreported')}
    alerts = response.get('alerts')
    if not isinstance(alerts, list):
        return result
    notices = []
    messages = {
        'this playlist does not exist': 'reported_missing',
        'the playlist does not exist': 'reported_missing',
        'this playlist is private': 'reported_private',
        'this playlist is unavailable': 'reported_unavailable',
        "this playlist isn't available": 'reported_unavailable',
    }
    for alert in alerts[:4]:
        renderer = next((name for name in ('alertRenderer', 'alertWithButtonRenderer')
                         if isinstance(alert, dict) and isinstance(alert.get(name), dict)), None)
        if renderer is None:
            notices.append({'renderer': 'unknown'})
            continue
        alert = alert[renderer]
        text = alert.get('text')
        message = text.get('simpleText') if isinstance(text, dict) else None
        if isinstance(text, dict) and message is None:
            runs = text.get('runs')
            if isinstance(runs, list) and len(runs) <= 8 and all(
                    isinstance(run, dict) and isinstance(run.get('text'), str)
                    and len(run['text']) <= 128 for run in runs):
                message = ''.join(run['text'] for run in runs)
        meaning = messages.get(message.strip().lower().rstrip('.')) if isinstance(message, str) and len(message) <= 128 else None
        severity = alert.get('type')
        notices.append({'renderer': renderer, 'type': severity if severity in ('ERROR', 'INFO', 'WARNING') else 'unknown',
                        'message_class': meaning or 'unclassified'})
    result['alert_sample'] = notices
    return result
