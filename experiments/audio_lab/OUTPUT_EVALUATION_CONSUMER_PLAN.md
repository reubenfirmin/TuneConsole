# Consumer continuation after the #587 Radio episode timeout

Round 3 published metadata pools in 275.832 seconds. All six Mix assemblies
returned in 1.29–1.34 seconds, but the first 24-track Chill Radio episode hit
the original 30-second consumer limit. Those Mix results had not yet been
written when the episode failed; their timings survive, not their ordered
outputs. Preserve this failure and do not claim that the original budgets pass.

Run the same diagnostic with a 180-second bound per consumer operation, retaining
600 seconds per publication and 1,800 per arm. No input, model, scoring, seed,
restriction, reference rule or repetition count changes. Record both original
and extended limit exceedances. This is output acquisition, not an acceptance
of the observed latency.

Reuse the completed metadata publication by copying its isolated database into
a new attempt. Verify its plan, profiles and publication heads, retain the prior
report hash, and never modify the failed attempt. Do not republish it. Signal and
combined still start from identical original account copies and build their own
intensity indexes. Save each completed output immediately; save partial Radio
prefixes on failure. If another required operation fails, retain the failure and
report the uncompleted conditions rather than substituting invented outputs.
