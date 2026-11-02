"""Compare every loaded MERT tensor with the pinned on-disk checkpoint."""
import os
import subprocess

from experiments.audio_lab_tools.encoder_assets import ROOT, MODEL, PYTHON


def test_all_checkpoint_tensors_load_exactly_despite_weight_norm_warning():
    script = '''
import sys
from pathlib import Path
import torch
from transformers import AutoModel
torch.manual_seed(708)
torch.set_num_threads(1)
model = AutoModel.from_pretrained(sys.argv[1], trust_remote_code=True, local_files_only=True)
saved = torch.load(Path(sys.argv[1]) / 'pytorch_model.bin', map_location='cpu', weights_only=True)
renames = {
    'encoder.pos_conv_embed.conv.parametrizations.weight.original0': 'encoder.pos_conv_embed.conv.weight_g',
    'encoder.pos_conv_embed.conv.parametrizations.weight.original1': 'encoder.pos_conv_embed.conv.weight_v',
}
actual = {renames.get(k, k): v for k, v in model.state_dict().items()}
assert len(actual) == len(saved) == 211
assert actual.keys() == saved.keys()
assert all(torch.equal(actual[k], v) for k, v in saved.items())
print('All 211 checkpoint tensors match exactly.')
'''
    env = {**os.environ, 'HF_HOME': str(ROOT / 'hf-cache'),
           'HF_MODULES_CACHE': str(ROOT / 'hf-cache/modules'),
           'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}
    result = subprocess.run([str(PYTHON), '-B', '-c', script, str(MODEL)],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'All 211 checkpoint tensors match exactly.' in result.stdout
