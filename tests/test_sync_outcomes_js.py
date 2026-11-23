"""Background retries stay quiet and only a successful sync refreshes Home."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest


def _evaluate(function, argument=None, *, hash=''):
    """Each state-only scenario gets an independent global object and timers."""
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is required for JavaScript state contracts')
    source = Path(__file__).parents[1] / 'src/yt_playlist/web/static/home.js'
    runner = """
        const fs = require('node:fs'), vm = require('node:vm');
        const input = JSON.parse(fs.readFileSync(0, 'utf8'));
        const refreshes = [], swaps = [];
        const root = {id: 'home', isConnected: true, querySelector: () => null};
        const fragment = '<section id="home">Updated Home</section>';
        const next = {dataset: {homePanel: 'directions'}, outerHTML: fragment, querySelector: () => null};
        const manifest = {textContent: JSON.stringify({styles: {}, scripts: {}, version: 'test'})};
        const responseElements = {'home': next, 'home-area-assets': manifest};
        const context = vm.createContext({setTimeout, clearTimeout, setInterval, clearInterval, setImmediate, console,
            AbortController, AbortSignal, URLSearchParams, location: {hash: input.hash},
            root, refreshes, swaps,
            async fetch(url, options) {
                if (url.startsWith('/sync/status/')) return context.fetchStatus(url);
                refreshes.push({url, headers: options.headers, abortable: options.signal instanceof AbortSignal});
                return {ok: true, text: async () => fragment};
            },
            DOMParser: class {
                parseFromString(html, type) {
                    if (html !== fragment || type !== 'text/html') throw new Error('Expected the Home response HTML');
                    return {getElementById: id => responseElements[id], querySelector: () => null};
                }
            },
            loadHomeAssets: async () => {},
            Alpine: {destroyTree() {}},
            htmx: {swap(target, html, options) { swaps.push({target: target.id, html, options}); }},
            addEventListener() {}, removeEventListener() {}});
        vm.runInContext('window = globalThis', context);
        vm.runInContext(fs.readFileSync(input.source, 'utf8'), context, {filename: input.source});
        context.argument = input.argument;
        Promise.resolve(vm.runInContext('(' + input.function + ')(argument)', context))
            .then(result => console.log(JSON.stringify(result)))
            .catch(error => {console.error(error); process.exitCode = 1;});
    """
    result = subprocess.run([node, '-e', runner], input=json.dumps({
        'source': str(source), 'function': function, 'argument': argument, 'hash': hash}),
        text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("outcome", ["complete", "partial", "skipped", "failed"])
@pytest.mark.parametrize("event_error", [False, True])
def test_home_respects_terminal_outcome(outcome, event_error):
    result = _evaluate("""async ({outcome, event_error}) => {
        const sources = [];
        window.EventSource = class {
            constructor() { sources.push(this); }
            close() { this.closed = true; }
        };
        const timers = [];
        window.setTimeout = (callback, delay) => timers.push(delay);
        const home = homeStatus();
        home.$el = root;
        home.attach(1);
        if (event_error) sources[0].onmessage({data: JSON.stringify({type: 'err', text: 'Temporary outage'})});
        sources[0].onmessage({data: JSON.stringify({type: 'end', outcome,
            error: outcome === 'complete' ? null : 'Incomplete sync'})});
        await new Promise(resolve => setImmediate(resolve));
        return {running: home.running, failed: home.failed, closed: sources[0].closed,
            refreshes, swaps, timers};
    }""", {"outcome": outcome, "event_error": event_error}, hash="#gen-wheelhouse")
    assert not result["running"] and result["closed"]
    assert result["timers"] == []
    if outcome == "complete" and not event_error:
        assert result["refreshes"] == [{"url": "/?panel=directions", "abortable": True,
                                        "headers": {"X-Home-Refresh": "sync"}}]
        assert result["swaps"] == [{"target": "home", "html": '<section id="home">Updated Home</section>',
                                    "options": {"swapStyle": "outerHTML", "settleDelay": 0}}]
        assert not result["failed"]
    else:
        assert result["failed"]
        assert result["refreshes"] == []
        assert result["swaps"] == []


@pytest.mark.parametrize("recovery", ["native", "status", "expired", "disposed"])
def test_home_reconnects_or_observes_retained_completion(recovery):
    result = _evaluate("""async recovery => {
        const sources = [], requests = [];
        window.EventSource = class {
            static CLOSED = 2;
            constructor() { this.readyState = 0; sources.push(this); }
            close() { this.closed = true; }
        };
        let resolveStatus;
        window.fetchStatus = url => { requests.push(url); return new Promise(resolve => {resolveStatus = resolve;}); };
        const home = homeStatus();
        home.$el = root;
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
        }
        await new Promise(resolve => setImmediate(resolve));
        // A duplicate terminal callback or active-job scan cannot refresh twice.
        if (recovery !== 'disposed') home.attach(7);
        return {interrupted, running: home.running, failed: home.failed, requests,
            refreshUrls: refreshes.map(request => request.url), swapCount: swaps.length,
            sources: sources.length, closed: !!stream.closed};
    }""", recovery, hash="#album-choices")
    assert result["interrupted"] == {"running": True, "failed": False, "closed": False}
    assert not result["failed"] and result["closed"]
    refresh_urls = ["/?panel=album-choices"] if recovery in {"native", "status"} else []
    assert result["refreshUrls"] == refresh_urls
    assert result["swapCount"] == len(refresh_urls)
    assert result["sources"] == 1
    if recovery != "disposed":
        assert not result["running"]
    assert result["requests"] == ([] if recovery == "native" else ["/sync/status/7"])


def test_home_status_fallback_keeps_live_job_through_transport_outage():
    result = _evaluate("""async () => {
        window.EventSource = class {
            static CLOSED = 2;
            constructor() { this.readyState = 2; window.stream = this; }
            close() {}
        };
        const home = homeStatus(); home.$el = root;
        let calls = 0;
        window.fetchStatus = async () => {
            if (++calls === 1) throw new Error('offline');
            return {status: 200, ok: true, json: async () => ({done: calls >= 3, outcome: 'complete'})};
        };
        home.attach(9); stream.onerror();
        await new Promise(resolve => setImmediate(resolve));
        await home.checkJob(9);
        const live = home.running;
        await home.checkJob(9);
        await new Promise(resolve => setImmediate(resolve));
        return {live, running: home.running, failed: home.failed,
            refreshUrls: refreshes.map(request => request.url), swapCount: swaps.length};
    }""")
    assert result == {"live": True, "running": False, "failed": False, "refreshUrls": ["/"], "swapCount": 1}
