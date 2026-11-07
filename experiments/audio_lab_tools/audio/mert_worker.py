"""Offline CPU worker; pinned MERT custom code and weights are local inputs."""
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
checkpoint = Path(sys.argv[1])
started = time.monotonic()
with redirect_stdout(sys.stderr):
    model = AutoModel.from_pretrained(str(checkpoint),trust_remote_code=True,local_files_only=True).eval()
    processor = Wav2Vec2FeatureExtractor.from_pretrained(str(checkpoint),local_files_only=True)
assert processor.sampling_rate==24000 and processor.do_normalize
print('READY '+json.dumps({'torch':torch.__version__,'transformers':transformers.__version__,
    'numpy':np.__version__,'python':sys.version,'parameters':sum(p.numel() for p in model.parameters()),
    'load_seconds':time.monotonic()-started,'threads':torch.get_num_threads(),
    'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024}),flush=True)
for line in sys.stdin:
    request = json.loads(line)
    audio = np.fromfile(request['pcm'],dtype='<f4')
    if audio.shape!=(240000,) or not np.isfinite(audio).all():raise ValueError('Expected exact finite ten-second PCM')
    started = time.monotonic()
    inputs = processor(audio,sampling_rate=24000,return_tensors='pt')
    with torch.inference_mode():
        outputs = model(**inputs,output_hidden_states=False)
        pooled = outputs.last_hidden_state.mean(dim=1)[0].cpu().numpy()
    if pooled.shape!=(768,) or not np.isfinite(pooled).all():raise ValueError('Invalid pooled representation')
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
    if rss>4*1024**3:raise RuntimeError('Frozen MERT worker RSS bound exceeded')
    print('RESULT '+json.dumps({'id':request['id'],'embedding':pooled.tolist(),'frames':outputs.last_hidden_state.shape[1],
        'seconds':time.monotonic()-started,'peak_rss_bytes':rss}),flush=True)
