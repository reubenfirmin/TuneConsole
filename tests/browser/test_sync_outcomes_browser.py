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


@pytest.mark.parametrize("recovery", ["native", "status", "expired", "disposed"])
def test_home_reconnects_or_observes_retained_completion(page, recovery):
    page.goto("about:blank")
    page.add_script_tag(path=str(Path(__file__).parents[2] / "src/yt_playlist/web/static/home.js"))
    result = page.evaluate("""async recovery => {
        const sources = [], refreshes = [], requests = [];
        window.EventSource = class {
            static CLOSED = 2;
            constructor() { this.readyState = 0; sources.push(this); }
            close() { this.closed = true; }
        };
        window.htmx = {ajax: (...args) => { refreshes.push(args); return Promise.resolve(); }};
        let resolveStatus;
        window.fetch = url => { requests.push(url); return new Promise(resolve => {resolveStatus = resolve;}); };
        const home = homeStatus();
        home.$el = {querySelector: () => null};
        home.attach(7);
        const stream = sources[0];
        stream.onerror();
        const interrupted = {running: home.running, failed: home.failed, closed: !!stream.closed};
        if (recovery === 'native') {
            stream.onmessage({data: JSON.stringify({type: 'end', outcome: 'complete', error: null})});
        } else {
            for (let i = 0; i < 4; ++i) stream.onerror();
            if (recovery === 'disposed') home.destroy();
            resolveStatus({status: recovery === 'expired' ? 404 : 200, ok: recovery !== 'expired',
                json: async () => ({done: true, outcome: 'complete', error: null})});
            await new Promise(resolve => setTimeout(resolve, 0));
        }
        // A duplicate terminal callback or active-job scan cannot refresh twice.
        if (recovery !== 'disposed') home.attach(7);
        return {interrupted, running: home.running, failed: home.failed, requests,
            refreshes: refreshes.length, sources: sources.length, closed: !!stream.closed};
    }""", recovery)
    assert result["interrupted"] == {"running": True, "failed": False, "closed": False}
    assert not result["failed"] and result["closed"]
    assert result["refreshes"] == (1 if recovery in {"native", "status"} else 0)
    assert result["sources"] == 1
    if recovery != "disposed":
        assert not result["running"]
    assert result["requests"] == ([] if recovery == "native" else ["/sync/status/7"])


def test_home_status_fallback_keeps_live_job_through_transport_outage(page):
    page.goto("about:blank")
    page.add_script_tag(path=str(Path(__file__).parents[2] / "src/yt_playlist/web/static/home.js"))
    result = page.evaluate("""async () => {
        window.EventSource = class {
            static CLOSED = 2;
            constructor() { this.readyState = 2; window.stream = this; }
            close() {}
        };
        const refreshes = [];
        window.htmx = {ajax: (...args) => {refreshes.push(args); return Promise.resolve();}};
        const home = homeStatus(); home.$el = {querySelector: () => null};
        let calls = 0;
        window.fetch = async () => {
            if (++calls === 1) throw new Error('offline');
            return {status: 200, ok: true, json: async () => ({done: calls >= 3, outcome: 'complete'})};
        };
        home.attach(9); stream.onerror();
        await new Promise(resolve => setTimeout(resolve, 0));
        await home.checkJob(9);
        const live = home.running;
        await home.checkJob(9);
        return {live, running: home.running, failed: home.failed, refreshes: refreshes.length};
    }""")
    assert result == {"live": True, "running": False, "failed": False, "refreshes": 1}
