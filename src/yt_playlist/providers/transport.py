"""One paced JSON request; adapters own URLs, credentials and response policy."""
import json
import urllib.request


def request_json(request, *, timeout, pace, breaker):
    pace()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except Exception as exc:
        breaker.record(exc)
        raise
    breaker.record()
    return data
