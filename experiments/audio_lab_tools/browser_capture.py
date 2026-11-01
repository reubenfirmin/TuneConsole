"""Measure actual element capture, browser resampling and frozen model inference.

Real supplied music plays through a controlled local response at the expected
player origin. Production capture/frame files run unchanged in an isolated
extension. Only the experiment keeps captured PCM/container bytes for decoder
comparison; ordinary application collection still discards its raw audio.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event, Thread
import time

import numpy as np
from threadpoolctl import threadpool_limits

from .external_checkpoint import RUN
from .external_experiment import ROOT, digest, save, stamp
from .reference_pipeline import examples, maximum_delta, process_sample
from .runtime_snapshot import ensure

WORK = RUN / 'browser-capture'
CAPTURE_FILES = ('audio_preview_capture.js', 'audio_preview_frame.js', 'audio_preview_frame.html')


def evaluate_js(cdp, source, context=None):
    result = cdp.send('Runtime.evaluate', {'expression': source, 'awaitPromise': True,
        'returnByValue': True, 'userGesture': False, **({'contextId': context} if context else {})})
    if 'exceptionDetails' in result:
        raise RuntimeError(str(result['exceptionDetails']))
    return result.get('result', {}).get('value')


def prepare_extension(directory):
    runtime, worker = ensure()
    destination = directory / 'extension'
    destination.mkdir()
    files = {}
    for name in CAPTURE_FILES:
        source = ROOT / 'extension' / name
        shutil.copyfile(source, destination / name)
        files[name] = digest(source)
    # Use the exact research extractor, rather than quietly mixing a current
    # production bundle with the already evaluated features.
    shutil.copytree(runtime, destination / 'audio_model', symlinks=True)
    original = json.loads((ROOT / 'extension/manifest.json').read_text())
    save(destination / 'manifest.json', {'manifest_version': 3, 'name': 'TuneConsole isolated capture audit',
        'version': '1.0', 'key': original['key'], 'permissions': ['scripting'],
        'host_permissions': ['https://music.youtube.com/*'],
        'background': {'service_worker': 'audit_background.js'},
        'content_security_policy': {'extension_pages': "script-src 'self' 'wasm-unsafe-eval'; object-src 'self'"},
        'web_accessible_resources': [{'resources': ['audio_preview_frame.html'], 'matches': ['https://music.youtube.com/*']}],
        'content_scripts': [{'matches': ['https://music.youtube.com/*'], 'js': ['audio_preview_capture.js'], 'run_at': 'document_idle'}]})
    (destination / 'audit_background.js').write_text('chrome.runtime.onMessage.addListener(() => {});\n')
    return destination, files, worker


def browser_sample(playwright, directory, audio, identifier, *, prepare=prepare_extension):
    extension, files, worker = prepare(directory)
    context = playwright.chromium.launch_persistent_context(directory / 'profile', headless=True,
        channel='chromium', ignore_default_args=['--mute-audio'], args=[
            '--disable-audio-output', '--disable-extensions-except=' + str(extension),
            '--load-extension=' + str(extension)])
    network = []
    def route(request):
        url = request.request.url
        if url == 'https://music.youtube.com/audit.wav':
            request.fulfill(content_type='audio/wav', body=audio.read_bytes())
        elif url == 'https://music.youtube.com/':
            request.fulfill(content_type='text/html', body='<title>Supplied music capture audit</title><audio src="/audit.wav"></audio>')
        elif url == 'https://music.youtube.com/favicon.ico':
            request.fulfill(status=204)
        elif url.startswith('chrome-extension://'):
            request.continue_()
        else:
            network.append(url)
            request.abort()
    context.route('**/*', route)
    context.add_init_script("""navigator.mediaDevices.getUserMedia=()=>{throw Error('Microphone forbidden');};
      navigator.mediaDevices.getDisplayMedia=()=>{throw Error('Display capture forbidden');};
      window.AudioContext=class {constructor(){throw Error('Live output forbidden');}};""")
    try:
        background = context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
        page = context.new_page()
        cdp = context.new_cdp_session(page)
        worlds = []
        cdp.on('Runtime.executionContextCreated', lambda event: worlds.append(event['context']))
        cdp.send('Runtime.enable')
        page.goto('https://music.youtube.com/')
        isolated = None
        deadline = time.monotonic() + 10
        while isolated is None and time.monotonic() < deadline:
            for world in worlds:
                if world.get('auxData', {}).get('isDefault') is False and world.get('origin', '').startswith('chrome-extension://'):
                    if evaluate_js(cdp, '!!window.__tcAudioPreviewCapture', world['id']):
                        isolated = world['id']
                        break
            if isolated is None:
                page.wait_for_timeout(50)
        if isolated is None:
            raise ValueError('Production capture script did not load in the extension world')
        evaluate_js(cdp, """(() => {
          window.auditEncoded=[]; window.auditModelPCM=null; window.auditDecoded=[]; window.auditStreams=[];
          const Recorder=MediaRecorder, Offline=OfflineAudioContext;
          window.MediaRecorder=class extends Recorder {
            constructor(...args){super(...args);this.addEventListener('dataavailable',e=>{if(e.data.size)auditEncoded.push(e.data)});}
          };
          const capture=HTMLMediaElement.prototype.captureStream;
          HTMLMediaElement.prototype.captureStream=function(){const stream=capture.call(this);auditStreams.push(stream);return stream;};
          window.OfflineAudioContext=class extends Offline {
            async decodeAudioData(...args){const pcm=await super.decodeAudioData(...args);auditDecoded.push(pcm);return pcm;}
            async startRendering(){const pcm=await super.startRendering();if(this.sampleRate===16000)auditModelPCM=Array.from(pcm.getChannelData(0));return pcm;}
          };
          window.AudioContext=class {constructor(){throw Error('Live output forbidden');}};
        })()""", isolated)
        page.locator('audio').evaluate("""media=>{
          window.auditPlayback={waiting:0,stalled:0,errors:0};
          for(const event of ['waiting','stalled','error'])media.addEventListener(event,()=>{
            auditPlayback[event==='error'?'errors':event]++;
          });
          return media.play();
        }""")
        session = context.browser.new_browser_cdp_session()
        roots = [row['id'] for row in session.send('SystemInfo.getProcessInfo')['processInfo'] if row['type'] == 'browser']
        browser_affinity = {str(pid): sorted(os.sched_getaffinity(pid)) for pid in roots}
        stopped, resources = Event(), []
        def sample():
            while not stopped.is_set():
                resources.append(process_sample(roots))
                stopped.wait(.1)
        sampler = Thread(target=sample, daemon=True)
        sampler.start()
        start = time.monotonic()
        try:
            result = background.evaluate("""async identifier => {
              const tab=(await chrome.tabs.query({url:'https://music.youtube.com/*'}))[0];
              if(!tab)throw Error('Audit player tab missing');
              return await new Promise((resolve,reject)=>{
                const port=chrome.tabs.connect(tab.id,{name:'tc-audio-preview-capture'});
                const timer=setTimeout(()=>{port.disconnect();reject(Error('Capture audit timed out'))},90000);
                port.onMessage.addListener(data=>{
                  if(data.error){clearTimeout(timer);port.disconnect();reject(Error(data.error));}
                  else if(data.type==='result'){clearTimeout(timer);port.disconnect();resolve(data.result);}
                });
                port.postMessage({type:'capture',id:identifier,video_id:'audit-song'});
              });
            }""", identifier)
        finally:
            stopped.set()
            sampler.join(timeout=5)
        resources.append(process_sample(roots))
        capture = evaluate_js(cdp, """(async()=>{
          const bytes=new Uint8Array(await new Blob(auditEncoded).arrayBuffer());
          let encoded='';for(let i=0;i<bytes.length;i+=8192)encoded+=String.fromCharCode(...bytes.subarray(i,i+8192));
          return {encoded:btoa(encoded),samples:auditModelPCM,decoded:auditDecoded.map(pcm=>({rate:pcm.sampleRate,
            channels:pcm.numberOfChannels,length:pcm.length,erased:Array.from({length:pcm.numberOfChannels},(_,i)=>pcm.getChannelData(i).every(v=>v===0)).every(Boolean)})),
            released:auditStreams.every(stream=>stream.getTracks().every(track=>track.readyState==='ended')),
            frame_removed:!document.querySelector('iframe[src*=audio_preview_frame]')};
        })()""", isolated)
        if network:
            raise ValueError('Capture audit attempted external network access: ' + repr(network))
        if not capture['released'] or not capture['frame_removed'] or not all(row['erased'] for row in capture['decoded']):
            raise ValueError('Production capture did not erase/release its buffers and tracks')
        container = directory / 'capture.webm'
        container.write_bytes(base64.b64decode(capture.pop('encoded')))
        signal = np.asarray(capture.pop('samples'), dtype='<f4')
        if signal.shape != (160000,) or not np.isfinite(signal).all():
            raise ValueError('Invalid captured model input')
        model_pcm = directory / 'browser.f32'
        signal.tofile(model_pcm)
        return {'result': result, 'capture': capture, 'container': str(container), 'model_pcm': str(model_pcm),
            'wall_seconds': time.monotonic() - start, 'capture_code_sha256': files,
            'playback': page.locator('audio').evaluate('media=>({...auditPlayback,position:media.currentTime,paused:media.paused,ended:media.ended,error:media.error?.code||null})'),
            'browser_root_cpu_affinity': browser_affinity,
            'chromium_version': context.browser.version,
            'peak_browser_pss_bytes': max(row['pss_bytes'] for row in resources),
            'peak_browser_summed_rss_bytes': max(row['rss_bytes'] for row in resources),
            'sampled_browser_cpu_seconds': max(0., resources[-1]['cpu_seconds'] - resources[0]['cpu_seconds']),
            'worker': str(worker), 'external_network_requests': network}
    finally:
        context.close()


def run(lifetime):
    from playwright.sync_api import sync_playwright
    ensure()
    target = WORK / stamp().replace(':', '').replace('+', '_')
    target.mkdir(parents=True)
    environment = {**os.environ, 'TF_NUM_INTRAOP_THREADS': '1', 'TF_NUM_INTEROP_THREADS': '1',
        'OMP_NUM_THREADS': '1', 'TF_CPP_MIN_LOG_LEVEL': '2', 'EXTERNAL_TFJS_NATIVE': '1'}
    rows = []
    capture_hashes = {name: digest(ROOT / 'extension' / name) for name in CAPTURE_FILES}
    with sync_playwright() as playwright:
        for index, row in enumerate(examples()):
            directory = target / str(index)
            directory.mkdir()
            audio = directory / 'playback.wav'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1',
                '-i', row['audio'], '-t', '25', '-ar', '48000', '-ac', '2', '-c:a', 'pcm_s16le', str(audio)],
                check=True, timeout=45, pass_fds=() if lifetime is None else (lifetime,))
            browser = browser_sample(playwright, directory, audio, 'capture-' + str(index))
            save(directory / 'browser.json', browser)
            if browser['capture_code_sha256'] != capture_hashes:
                raise ValueError('Production capture files changed during the experiment')
            reference_pcm = directory / 'reference.f32'
            # WebM has millisecond packet timestamps: ffmpeg -t 10 can emit
            # several extra resampled frames. Trim decoded samples, exactly as
            # the browser's fixed-size offline render does; never pad short audio.
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1',
                '-filter_threads', '1', '-i', browser['container'], '-ac', '1',
                '-af', 'aresample=16000,atrim=end_sample=160000', '-f', 'f32le',
                '-y', str(reference_pcm)], check=True, timeout=45,
                pass_fds=() if lifetime is None else (lifetime,))
            if reference_pcm.stat().st_size != 160000 * 4:
                raise ValueError('Captured container lacks ten seconds of real audio')
            first = np.fromfile(browser['model_pcm'], dtype='<f4')
            second = np.fromfile(reference_pcm, dtype='<f4')
            inference = {}
            for name, path in [('matched_browser_pcm', Path(browser['model_pcm'])), ('reference_resampled_pcm', reference_pcm)]:
                request = {'id': row['id'], 'model_pcm': str(path.resolve()),
                           'native_output': str((directory / (name + '.native.json')).resolve())}
                request_path = directory / 'native-request.json'
                save(request_path, request)
                with (directory / 'native.log').open('a') as log:
                    subprocess.run([sys.executable, '-B', '-m', 'experiments.audio_lab_tools.reference_pipeline',
                        '--native', str(request_path)], stdout=log, stderr=log, env=environment,
                        check=True, timeout=120, pass_fds=() if lifetime is None else (lifetime,))
                inference[name] = json.loads(Path(request['native_output']).read_text())
            prediction = browser['result']['prediction']
            matched = inference['matched_browser_pcm']
            reference = inference['reference_resampled_pcm']
            rows.append({'id': row['id'], 'audio_sha256': row['audio_sha256'], 'browser': browser,
                'captured_container_sha256': digest(browser['container']),
                'captured_pcm_sha256': digest(browser['model_pcm']),
                'reference_pcm_sha256': digest(reference_pcm),
                'resampling_signal_delta': {'maximum_absolute': float(np.max(np.abs(first - second))),
                    'root_mean_squared': float(np.sqrt(np.mean((first - second) ** 2)))},
                'native_browser_matched_pcm_delta': {
                    'embedding': maximum_delta(matched['embedding'], prediction['embedding']['values']),
                    'tags': maximum_delta(matched['scores'], prediction['scores']),
                    'moods': maximum_delta(matched['traits'], {key: value['score'] for key, value in prediction['moods']['traits'].items()})},
                'resampling_embedding_delta': maximum_delta(matched['embedding'], reference['embedding'])})
            save(target / 'progress.json', {'completed': rows})
    report = {'created_at': stamp(), 'script_sha256': digest(__file__), 'cases': rows,
        'scope': 'Actual HTMLMediaElement.captureStream and PCM MediaRecorder, unchanged production decode/downmix/resample and extension-frame handoff, frozen research worker. Native inference also consumes the exact captured model PCM; independent ffmpeg decodes the same captured container.',
        'limitations': ['Controlled virtual player, supplied music, four recordings; this does not exercise signed-in YouTube playback, automatic application scheduling, MV3 suspension, DRM or battery consumption.',
            'Minimal experiment extension orchestrates capture; the ordinary service worker and app bridge are not under test.',
            'Raw audio retention is restricted to ignored experiment artifacts for decoder comparison.',
            'Browser memory samples are 100 ms process-tree observations; summed RSS double counts shared memory.']}
    save(target / 'report.json', report)
    print(json.dumps({'browser_capture_report': str(target / 'report.json'),
        'cases': [{key: row[key] for key in ('id', 'native_browser_matched_pcm_delta', 'resampling_embedding_delta')} for row in rows]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inspect', action='store_true', help='Inspect retained capture files without running another browser')
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    if args.inspect:
        for path in sorted(WORK.glob('*/[0-9]')):
            container = path / 'capture.webm'
            details = json.loads(subprocess.check_output(['ffprobe', '-v', 'quiet', '-show_format',
                '-show_streams', '-of', 'json', str(container)])) if container.is_file() else None
            print(json.dumps({'path': str(path), 'f32_samples': {
                item.name: item.stat().st_size // 4 for item in path.glob('*.f32')},
                'container': details}), flush=True)
        return
    with threadpool_limits(limits=1):
        run(None)


if __name__ == '__main__':
    main()
