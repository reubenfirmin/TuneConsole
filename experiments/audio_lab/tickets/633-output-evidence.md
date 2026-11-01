Related diagnostic evidence from #587, not a reproduced root cause of this live launch failure.

The isolated retained account has 14,332 recordings. Its full frozen runtime is
`experiments/audio_lab/local/external-20261028/checkpoint-1/library-replay/runtime-20261030T195411.511565Z`
(1,040 files; manifest SHA256 `160c16c69627533785c94624a6efcbcc5acbdd4b6ece84a3d8af2979014ad7e2`).
The native schema-93 database was exported read-only; all execution uses private
copies. This is the frozen working-tree composition, not a claim about which code
the live server loaded.

The three evidence arms independently assemble six four-card Mix Tapes and
24-song Radio episodes at Chill, midpoint and Full on. All complete-capture
outputs fill. Mix assembly is about 1.3 seconds; Radio episode assembly takes
about 43–52 seconds at the extremes and 7 seconds at midpoint. The existing
benchmark's retune scope takes roughly 48–57 seconds, almost entirely CPU time:
construct the current and retuned `RadioCandidates`, then build four plans with
`RadioDirections.update`. HTTP handling, session checkpoint persistence, remote
playlist writes and extension navigation are outside this measurement.

The first candidate publication exceeded #587's original 180-second budget.
Separately declared continuations complete publication in about 265–276 seconds;
the original failure is retained. These facts justify examining candidate and
direction construction while tracing this ticket's complete start path. They
do not establish a deadlock, actual request latency, successful remote playback,
or that this frozen runtime still matches the current integration.

Exact per-operation wall/CPU times, source identities, retained failures and
limits are in `experiments/audio_lab/OUTPUT_EVALUATION_RESULTS.md` and
`experiments/audio_lab/local/output-evaluation-587-20261030/round-4/`.
No live-database test or mutation, full suite, commit or push was performed.
