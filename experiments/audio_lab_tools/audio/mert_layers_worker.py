"""Pinned offline MERT worker exposing fixed intermediate and final states."""
from contextlib import redirect_stdout
import json
from pathlib import Path
import resource
import sys
import time

import numpy as np
import torch
import transformers
from transformers import AutoModel, Wav2Vec2FeatureExtractor


torch.set_num_threads(1)
torch.set_num_interop_threads(1)
torch.manual_seed(708)
torch.use_deterministic_algorithms(True)
started = time.monotonic()
checkpoint = Path(sys.argv[1])
with redirect_stdout(sys.stderr):
    model = AutoModel.from_pretrained(str(checkpoint), trust_remote_code=True,
                                     local_files_only=True).eval()
    processor = Wav2Vec2FeatureExtractor.from_pretrained(str(checkpoint), local_files_only=True)
assert processor.sampling_rate == 24000 and processor.do_normalize
assert model.config.num_hidden_layers == 12
print('READY ' + json.dumps({'torch': torch.__version__, 'transformers': transformers.__version__,
    'numpy': np.__version__, 'python': sys.version, 'threads': torch.get_num_threads(),
    'load_seconds': time.monotonic() - started,
    'indexing': 'state 0 before transformer blocks; states 5/6 after blocks 5/6'}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    audio = np.fromfile(request['pcm'], dtype='<f4')
    if audio.shape != (240000,) or not np.isfinite(audio).all():
        raise ValueError('Expected exact finite ten-second PCM')
    started = time.monotonic()
    inputs = processor(audio, sampling_rate=24000, return_tensors='pt')
    with torch.inference_mode():
        output = model(**inputs, output_hidden_states=True)
        states = output.hidden_states
        if len(states) != 13:
            raise ValueError('Expected input plus twelve block outputs')
        if not torch.equal(states[-1], output.last_hidden_state):
            raise ValueError('Final hidden-state indexing mismatch')
        final = states[-1].mean(dim=1)[0].cpu().numpy()
        middle = np.concatenate([states[i].mean(dim=1)[0].cpu().numpy() for i in (5, 6)])
    if final.shape != (768,) or middle.shape != (1536,) or not np.isfinite(middle).all():
        raise ValueError('Invalid pooled representation')
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    if rss > 4 * 1024**3:
        raise RuntimeError('Worker RSS bound exceeded')
    print('RESULT ' + json.dumps({'id': request['id'], 'embedding': middle.tolist(),
        'final_embedding': final.tolist(), 'hidden_states': len(states),
        'frames': states[-1].shape[1], 'seconds': time.monotonic() - started,
        'peak_rss_bytes': rss}), flush=True)
