"""Real recovery/polling code with controlled network responses and timers."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('view', ['home', 'onboarding'])
@pytest.mark.parametrize('network', ['deferred', 'active'])
@pytest.mark.parametrize('leaving', ['destroy', 'app-page-leaving', 'pagehide'])
def test_home_has_one_read_and_cancels_it_when_disposed(view, network, leaving):
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is required for JavaScript state contracts')
    runner = r"""
        const fs = require('node:fs'), vm = require('node:vm');
        const input = JSON.parse(fs.readFileSync(0, 'utf8'));
        const window = new EventTarget(), document = new EventTarget();
        const pending = new Map(), polls = new Map(), requests = [];
        let id = 0, aborted = 0;
        window.playbackStatus = {start() {}, snapshot: null};
        window.fetch = async (url, options) => {
            requests.push(url);
            if (input.network === 'deferred') return new Response('', {status: 503,
                headers: {'X-Read-Pending': 'true', 'Retry-After': '2'}});
            return new Promise((resolve, reject) => {
                options.signal.addEventListener('abort', () => {
                    aborted++; reject(options.signal.reason);
                }, {once: true});
            });
        };
        const context = vm.createContext({window, document, Response, AbortController, Event,
            setTimeout(callback) {pending.set(++id, callback); return id;},
            clearTimeout(id) {pending.delete(id);},
            setInterval(callback) {polls.set(++id, callback); return id;},
            clearInterval(id) {polls.delete(id);},
        });
        vm.runInContext(fs.readFileSync(input.root + '/read-recovery.js', 'utf8'), context);
        context.fetch = window.fetch;
        vm.runInContext(fs.readFileSync(input.root + '/home.js', 'utf8'), context);
        const state = vm.runInContext(input.view === 'home' ? 'homeStatus()' : 'onboardingArrival()', context);
        state.$el = Object.assign(new EventTarget(), {querySelector: () => null, closest: () => null});
        state.frameIntro = () => {};
        document.documentElement = {dataset: {}};
        const settle = () => new Promise(resolve => setImmediate(resolve));
        (async () => {
            state.init();
            await settle();
            for (let tick = 0; tick < 4; tick++) {
                if (input.view === 'home') [...polls.values()][0]();
                else state.refreshArrival();
                await settle();
            }
            const before = {requests: requests.length, retries: pending.size};
            const restored = [];
            if (input.leaving === 'pagehide') {
                for (let cycle = 0; cycle < 2; cycle++) {
                    window.dispatchEvent(Object.assign(new Event('pagehide'), {persisted: true}));
                    await settle();
                    const hidden = {retries: pending.size, polls: polls.size,
                        disposed: state._disposed, suspended: state._suspended};
                    window.dispatchEvent(Object.assign(new Event('pageshow'), {persisted: true}));
                    await settle();
                    for (let tick = 0; tick < 4; tick++) {
                        if (input.view === 'home') [...polls.values()][0]();
                        else state.refreshArrival();
                        await settle();
                    }
                    restored.push({hidden, requests: requests.length, retries: pending.size,
                        polls: polls.size, suspended: state._suspended});
                }
                state.destroy();
            } else if (input.leaving === 'destroy') state.destroy();
            else window.dispatchEvent(new Event(input.leaving));
            await settle();
            for (const callback of pending.values()) callback();
            await settle();
            console.log(JSON.stringify({before, requests: requests.length, retries: pending.size,
                polls: polls.size, aborted, disposed: state._disposed, restored}));
        })().catch(error => {console.error(error); process.exitCode = 1;});
    """
    result = subprocess.run([node, '-e', runner], input=json.dumps({
        'root': str(Path(__file__).parents[1] / 'src/yt_playlist/web/static'),
        'view': view, 'network': network, 'leaving': leaving}),
        text=True, capture_output=True, check=True, timeout=10)
    state = json.loads(result.stdout)
    assert state['before'] == {'requests': 1, 'retries': int(network == 'deferred')}
    requests = 3 if leaving == 'pagehide' else 1
    assert state['requests'] == requests and state['retries'] == 0 and state['polls'] == 0
    assert state['disposed']
    assert state['aborted'] == requests * int(network == 'active')
    assert state['restored'] == ([{
        'hidden': {'retries': 0, 'polls': 0, 'disposed': False, 'suspended': True},
        'requests': cycle + 2, 'retries': int(network == 'deferred'),
        'polls': int(view == 'home'), 'suspended': False,
    } for cycle in range(2)] if leaving == 'pagehide' else [])


@pytest.mark.parametrize('phase', ['backoff', 'active-retry'])
@pytest.mark.parametrize('retry_after', ['2', 'unavailable'])
@pytest.mark.parametrize('target,leaving', [
    ('url', 'pagehide'), ('request', 'pagehide'), ('caller', 'pagehide'),
    ('request', 'caller'), ('caller', 'caller'),
])
def test_retry_transport_observes_document_and_caller_cancellation(target, leaving, phase, retry_after):
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is required for JavaScript state contracts')
    runner = r"""
        const fs = require('node:fs'), vm = require('node:vm');
        const input = JSON.parse(fs.readFileSync(0, 'utf8'));
        const window = new EventTarget(), document = new EventTarget();
        const caller = new AbortController(), timers = new Map(), delays = [];
        let id = 0, calls = 0, aborted = 0, active;
        window.fetch = async (_url, options) => {
            calls++;
            if (calls === 1) return new Response('', {status: 503,
                headers: {'X-Read-Pending': 'true', 'Retry-After': input.retry_after}});
            active = options.signal;
            return new Promise((resolve, reject) => active.addEventListener('abort', () => {
                aborted++; reject(active.reason);
            }, {once: true}));
        };
        const context = vm.createContext({window, document, AbortController, Event,
            setTimeout(callback, milliseconds) {delays.push(milliseconds); timers.set(++id, callback); return id;},
            clearTimeout(id) {timers.delete(id);},
        });
        vm.runInContext(fs.readFileSync(input.script, 'utf8'), context);
        const settle = () => new Promise(resolve => setImmediate(resolve));
        (async () => {
            const url = 'http://controlled.invalid/read';
            const target = input.target === 'request' ? new Request(url, {signal: caller.signal}) : url;
            const options = input.target === 'caller' ? {signal: caller.signal} : {};
            let outcome = 'pending';
            const pending = window.fetch(target, options).then(() => {outcome = 'loaded';},
                error => {outcome = error.name;});
            try {
                await settle();
                if (input.phase === 'active-retry') {
                    const [id, retry] = [...timers][0];
                    timers.delete(id); retry(); await settle();
                }
                if (input.leaving === 'caller') caller.abort();
                else window.dispatchEvent(new Event('pagehide'));
                await settle();
                console.log(JSON.stringify({calls, aborted, outcome, timers: timers.size,
                    activeAborted: active?.aborted, delays}));
            } finally {
                caller.abort(); window.dispatchEvent(new Event('pagehide'));
                await pending;
            }
        })().catch(error => {console.error(error); process.exitCode = 1;});
    """
    result = subprocess.run([node, '-e', runner], input=json.dumps({
        'script': str(Path(__file__).parents[1] / 'src/yt_playlist/web/static/read-recovery.js'),
        'target': target, 'leaving': leaving, 'phase': phase, 'retry_after': retry_after}),
        text=True, capture_output=True, check=True, timeout=10)
    state = json.loads(result.stdout)
    assert state['outcome'] == 'AbortError' and state['timers'] == 0
    assert state['delays'] == [2000]
    assert state['calls'] == (2 if phase == 'active-retry' else 1)
    assert state['aborted'] == int(phase == 'active-retry')
    if phase == 'active-retry':
        assert state['activeAborted']
