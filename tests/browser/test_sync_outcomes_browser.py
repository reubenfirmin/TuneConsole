"""Background retries stay quiet and only a successful sync refreshes Home."""
from pathlib import Path

import pytest


@pytest.mark.parametrize("outcome", ["complete", "partial", "skipped", "failed"])
@pytest.mark.parametrize("event_error", [False, True])
def test_home_respects_terminal_outcome(page, outcome, event_error):
    page.goto("about:blank")
    page.add_script_tag(path=str(Path(__file__).parents[2] /
                                "src/yt_playlist/web/static/home.js"))
    result = page.evaluate("""({outcome, event_error}) => {
        const sources = [];
        window.EventSource = class {
            constructor() { sources.push(this); }
            close() { this.closed = true; }
        };
        const timers = [];
        window.setTimeout = (callback, delay) => timers.push(delay);
        const refreshes = [];
        window.htmx = {ajax: (...args) => { refreshes.push(args); return Promise.resolve(); }};
        const home = homeStatus();
        home.$el = {querySelector: () => null};
        home.attach(1);
        if (event_error) sources[0].onmessage({data: JSON.stringify({type: 'err', text: 'Temporary outage'})});
        sources[0].onmessage({data: JSON.stringify({type: 'end', outcome,
            error: outcome === 'complete' ? null : 'Incomplete sync'})});
        return {running: home.running, failed: home.failed, closed: sources[0].closed,
            refreshes, timers};
    }""", {"outcome": outcome, "event_error": event_error})
    assert not result["running"] and result["closed"]
    assert result["timers"] == []
    if outcome == "complete" and not event_error:
        assert result["refreshes"] == [["GET", "/", {
            "target": "#home", "select": "#home", "swap": "outerHTML"}]]
        assert not result["failed"]
    else:
        assert result["failed"]
        assert result["refreshes"] == []
