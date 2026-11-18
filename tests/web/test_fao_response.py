"""Managed HTTP content keeps custody until delivery finishes or is cancelled."""
from pathlib import Path

import anyio
import pytest

from yt_playlist.core import fao
from yt_playlist.web.fao_response import response


@pytest.mark.parametrize('compressed', [False, True])
@pytest.mark.parametrize('ending', ['complete', 'cancel', 'disconnect'])
@pytest.mark.parametrize('request_range', [None, b'bytes=100-70100'])
def test_delivery_holds_original_and_materialized_file_until_exit(tmp_path, compressed, ending, request_range):
    original = tmp_path / 'audio.bin'
    payload = bytes(range(256)) * 1024
    if compressed:
        fao.write(original, payload)
    else:
        original.write_bytes(payload)
    delivery = response(fao.read(original))
    delivered, leased = [], []

    async def run():
        async with anyio.create_task_group() as tasks:
            async def receive():
                await anyio.sleep_forever()

            async def send(message):
                assert message['type'] != 'http.response.pathsend'
                if message['type'] == 'http.response.start':
                    assert message['status'] == (206 if request_range else 200)
                if message['type'] == 'http.response.body':
                    path = Path(delivery.path)
                    leased.append(path)
                    assert path != original and path.exists()
                    assert not fao.delete(original)
                    delivered.append(message['body'])
                    if ending == 'cancel':
                        tasks.cancel_scope.cancel()
                        await anyio.sleep(0)
                    if ending == 'disconnect':
                        raise OSError('controlled disconnected receiver')

            try:
                await delivery({'type': 'http', 'method': 'GET', 'headers': [(b'range', request_range)] if request_range else [],
                    'extensions': {'http.response.pathsend': {}}}, receive, send)
            except OSError:
                assert ending == 'disconnect'

    anyio.run(run)
    assert leased and all(not path.exists() for path in leased)
    assert fao.delete(original)
    if ending == 'complete':
        assert b''.join(delivered) == (payload[100:70101] if request_range else payload)
