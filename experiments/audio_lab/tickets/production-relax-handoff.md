Author implementation handoff for #629; independent acceptance remains open.

Implemented the frozen #617 fixed blend as default active Relax suitability,
with per-excerpt clipped native predictions averaged before the fixed mappings.
The eleven source heads are exported exactly as five multi-output ridge models;
no refitting, learned-weight substitution or local-label scoring occurred.
Cruise, Release, shared classification and intensity retain their behavior.

Evidence: `experiments/audio_lab/RELAX_PRODUCTIONIZATION.md` and retained
`experiments/audio_lab/local/relax-production-20261030/`.

- `./experiments/run.py relax-package --directory experiments/audio_lab/local/relax-production-20261030/package-1 --install`: passed. 2,032 external recording/preparation cases and 159 actual-capture cases across 84 songs. Every native output and final blend matched independent coefficient arithmetic within `6.245004513516506e-16`. This proves export fidelity, not new held-out accuracy.
- `uv run pytest tests/test_audio_vibe_relax.py tests/test_audio_vibe_cruise.py tests/test_audio_vibe_release.py tests/test_audio_vibe_external.py tests/test_audio_vibe_model.py tests/test_audio_vibe_embeddings.py tests/rec/test_vibe_prediction.py`: **168 passed**, 122.37 seconds, one existing Starlette/httpx warning. Controlled fixtures cover invalid artifacts/weights/aggregation, provenance, default activation, missing metadata, partial/continuing capture, one recording vote, immutable history, human feedback, annotation isolation and exact rollback.
- `./experiments/run.py relax-production --directory experiments/audio_lab/local/relax-production-20261030/replay-2`: passed with unchanged product Python files. New isolated database, actual frozen captures, network blocked. All 73 complete captures gained the new score; 11 partial captures retained fallback. Home consumed prepared Relax suitability. Rollout 2.506 seconds of work, maximum slice 0.526 seconds; rollback restored every prior fit exactly. Shared classification/intensity and other vibes were unchanged. `replay-1` passed behavioral assertions but is retained as incomplete because unrelated `repos/history.py` changed during its run.
- Offline wheel built through the declared hatchling backend with cached dependencies, versions in `wheel-1/build.json`. `./experiments/run.py relax-wheel --directory experiments/audio_lab/local/relax-production-20261030/installed-2 --wheel experiments/audio_lab/local/relax-production-20261030/wheel-1/yt_playlist-0.1.17.dev29+g2a2af74d1.d20261030-py3-none-any.whl`: **159 cases passed**, exact scores, all three default specialist modes, no experiment imports or network access. Fifteen production files checked byte for byte. The first extraction exhausted repository disk space; only that incomplete extraction was removed, and the successful check used temporary `/tmp` storage.

Artifact: 113,117 bytes, SHA256
`4fc43b28a71304ababa8514100f8080752f308a37f397681b127332febb5fbbd`.
Warm cached-feature inference: median 0.442 ms, maximum 2.637 ms; excludes audio
extraction and encoder cost. Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1,
threadpoolctl 3.7.0. Research execution used one numerical thread.

Package receipt SHA256:
`ed82bc0cf51649b8b7fb408c68e43bb73e012f4e144e69430f7f4b743d4edeba`.
Stable preparation report SHA256:
`dda3c99468aa31fe34e485bbc975b8ea6b8f27eb194277f484b610d4b8cf065c`.
Wheel SHA256:
`4b6c841aad07683386b282bb046e387ee729eaf9025955f52664974f2fd91497`.

Rollback: `YT_PLAYLIST_AUDIO_VIBE_RELAX_MODE=off` in the environment or
`.env.local`; bounded preparation rescores existing measurements without new
audio capture. Missing or incompatible artifacts preserve the other scorers.

These are author observations and controlled/actual-capture checks, not a
full-account or rendered-output acceptance. Reuse #424 capture evidence;
#573 retains account/rendered acceptance, #624 its existing consumer failures,
and #405/#587 whole-output evaluation. Preserve #617's uncertainty and the
source-terms status recorded in the packaged notice. No global suite or test
lock, live-database writes, commit, push or app deployment was performed.
