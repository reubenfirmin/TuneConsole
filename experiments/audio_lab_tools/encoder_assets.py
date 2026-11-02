"""Pinned public MERT assets and a private CPU-only research environment."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.request

from .external_experiment import LAB, digest, save, stamp
from .focus_experiment import load

ROOT = LAB/'local/encoder-708'
MODEL = ROOT/'checkpoint'
PYTHON = ROOT/'venv/bin/python'
REPO = 'm-a-p/MERT-v1-95M'
LIMIT = 512*1024**2


def metadata():
    ROOT.mkdir(parents=True,exist_ok=True)
    path = ROOT/'upstream.json'
    if not path.exists():
        with urllib.request.urlopen('https://huggingface.co/api/models/'+REPO+'?blobs=true',timeout=45) as response:
            save(path,json.load(response))
    value = load(path)
    print(json.dumps({'revision':value['sha'],'files':[{'name':r['rfilename'],'size':r.get('size'),'lfs':r.get('lfs')} for r in value['siblings']]}),flush=True)


def download():
    upstream = load(ROOT/'upstream.json')
    revision = upstream['sha']
    names = {'config.json','preprocessor_config.json','configuration_MERT.py','modeling_MERT.py','pytorch_model.bin','README.md'}
    selected = [r for r in upstream['siblings'] if r['rfilename'] in names]
    if {r['rfilename'] for r in selected} != names:
        raise ValueError('Inspect changed upstream asset inventory before downloading')
    MODEL.mkdir(exist_ok=True)
    received, records = 0, []
    started = time.monotonic()
    for row in selected:
        path = MODEL/row['rfilename']
        expected = (row.get('lfs') or {}).get('sha256')
        if not path.exists():
            url = f'https://huggingface.co/{REPO}/resolve/{revision}/{row["rfilename"]}'
            request = urllib.request.Request(url,headers={'User-Agent':'TuneConsole-research/1'})
            try:
                with urllib.request.urlopen(request,timeout=90) as response, path.open('wb') as stream:
                    if int(response.headers.get('Content-Length','0'))>LIMIT-received:
                        raise ValueError('Checkpoint download byte bound')
                    while block := response.read(1024**2):
                        received += len(block)
                        if received>LIMIT or time.monotonic()-started>1800:
                            raise ValueError('Checkpoint download resource bound')
                        stream.write(block)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        if path.stat().st_size!=row['size'] or expected and digest(path)!=expected:
            raise ValueError('Pinned checkpoint size/LFS SHA256 mismatch: '+path.name)
        records.append({'name':path.name,'bytes':path.stat().st_size,'sha256':digest(path),'lfs_sha256':expected})
        print(json.dumps({'checkpoint_asset':path.name,'bytes':path.stat().st_size}),flush=True)
    save(ROOT/'assets.json',{'created_at':stamp(),'repository':REPO,'revision':revision,'files':records,
        'network_bytes':received,'seconds':time.monotonic()-started,
        'provenance':'Official model card: 95M parameters, 20k training hours, 24 kHz, 12x768. Exact recording pretraining overlap unknown. Weights CC-BY-NC-4.0; official implementation Apache-2.0.'})


def environment():
    cache = str(ROOT/'uv-cache')
    if not PYTHON.exists():
        subprocess.run(['uv','--cache-dir',cache,'venv','--python','3.12',str(ROOT/'venv')],check=True)
    subprocess.run(['uv','--cache-dir',cache,'pip','install','--python',str(PYTHON),
        '--index-url','https://download.pytorch.org/whl/cpu','torch==2.6.0'],check=True,timeout=900)
    subprocess.run(['uv','--cache-dir',cache,'pip','install','--python',str(PYTHON),
        'transformers==4.38.2','numpy==1.26.4'],check=True,timeout=900)
    freeze = subprocess.check_output(['uv','--cache-dir',cache,'pip','freeze','--python',str(PYTHON)],text=True)
    (ROOT/'requirements.lock').write_text(freeze)
    print(freeze,flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['metadata','download','environment'])
    args = parser.parse_args()
    {'metadata':metadata,'download':download,'environment':environment}[args.action]()


if __name__=='__main__':main()
