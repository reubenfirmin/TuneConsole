"""Small receipt-classification contracts for the actual song action client."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.quiet_validation_support import DIAGNOSTIC


@pytest.mark.parametrize('status,code', [
    pytest.param(400, None, id='null-validation-code'),
    pytest.param(422, 'constructor', id='inherited-validation-code'),
    pytest.param(503, 'account_disconnected', id='nonvalidation-status'),
])
def test_post_rejects_untrusted_validation_receipts(status, code):
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is required for JavaScript state contracts')
    source = Path(__file__).parents[1] / 'src/yt_playlist/web/static/song-actions.js'
    runner = """
        const fs = require('node:fs'), vm = require('node:vm');
        const input = JSON.parse(fs.readFileSync(0, 'utf8'));
        const calls = [];
        const context = vm.createContext({
            console: {debug() {}}, AbortSignal, setTimeout, clearTimeout,
            document: {addEventListener() {}, documentElement: {dataset: {}}},
            fetch: async (url, options) => {
                calls.push({url, method: options.method, body: options.body});
                return {ok: false, status: input.status,
                    json: async () => ({code: input.code, detail: input.diagnostic})};
            }
        });
        vm.runInContext(fs.readFileSync(input.source, 'utf8'), context, {filename: input.source});
        Promise.resolve(vm.runInContext(
            "songActions().json('/songs/create-playlist', {name: 'Retained draft'})", context))
            .then(() => {throw Error('Untrusted receipt was accepted');})
            .catch(error => console.log(JSON.stringify({message: error.message, calls})));
    """
    result = subprocess.run([node, '-e', runner], input=json.dumps({
        'source': str(source), 'status': status, 'code': code, 'diagnostic': DIAGNOSTIC}),
        text=True, capture_output=True, timeout=10, check=True)
    assert json.loads(result.stdout) == {
        'message': 'This change was not confirmed. Review it before trying again.',
        'calls': [{'url': '/songs/create-playlist', 'method': 'POST',
                   'body': json.dumps({'name': 'Retained draft'}, separators=(',', ':'))}],
    }
