"""Worker requests have a deadline independently of the window fetch wrapper."""
from pathlib import Path
import subprocess


def test_chart_worker_bounds_stalled_reads_and_processes_the_current_selection():
    worker = Path(__file__).parents[1] / 'src/yt_playlist/web/static/listening-analytics-worker.js'
    result = subprocess.run(['node', '-e', r'''
        const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
        const results = [];
        let stalled = true, calls = 0;
        const context = vm.createContext({
            self: {postMessage: message => results.push(message)},
            AbortController,
            AbortSignal: {any: AbortSignal.any, timeout(milliseconds) {
                assert.equal(milliseconds, 20000);
                return AbortSignal.timeout(20);
            }},
            fetch: async (_url, {signal}) => {
                calls++;
                if (!stalled) return new Response(JSON.stringify({rows: [], catalog: {}}));
                return new Promise((_resolve, reject) => signal.addEventListener('abort',
                    () => reject(signal.reason), {once: true}));
            },
        });
        vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
        // Keep Node alive while its unreferenced AbortSignal timer expires.
        const keepAlive = setInterval(() => {}, 1000);
        (async () => {
            try {
                const encoded = JSON.stringify({schema: 2, today: '2026-11-03', columns: [],
                    chunks: [{key: 'one', from: '2026-11-01', until: '2026-11-03'}]});
                const filters = {window: '7d', period: 'day'};
                await context.self.onmessage({data: {id: 1, encoded, filters}});
                assert.deepEqual(JSON.parse(JSON.stringify(results)), [{id: 1, unavailable: true}]);
                const obsolete = context.self.onmessage({data: {id: 2, filters}});
                stalled = false;
                await context.self.onmessage({data: {id: 3, filters}});
                await obsolete;
                assert.equal(results.length, 2);
                assert.equal(results[1].id, 3);
                assert.ok(results[1].result);
                await context.self.onmessage({data: {id: 4, filters: {...filters, window: 'custom'}}});
                assert.match(results[2].error, /Choose valid dates/);
                assert.equal(calls, 3);
            } finally {clearInterval(keepAlive);}
        })().catch(error => {console.error(error); process.exitCode = 1;});
    ''', str(worker)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
