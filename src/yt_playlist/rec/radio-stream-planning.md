# Radio stream planning

Each of the four streams is a hypothesis about how to continue the current
playlist. Its anchor, preferred styles and evolving proposed history describe
a musical trajectory. Streams may share styles; their continuations should
offer meaningful alternatives. Discovery/Comfort, Eclectic/Themed and
Chill/Full On steer all four hypotheses.

## Shared facts, separate decisions

One regeneration shares a committed view of catalog eligibility, genre meanings,
prepared recommendation membership, musical profiles and embedding vectors.
Retrieve candidate pages in bounded batches and retain reusable facts for that
planning pass. Fetch display explanations only when presentation needs them.
Release planning caches after the pass; subsequent passes observe catalog and
feedback changes.

Planning retains at most 256 hydrated rank pages and 8,192 entries in each
recording-fact cache. Overlapping pages share current catalog facts, profiles
and score lookups. Listening counts use only each batch's subjects, retaining
the canonical account/day deduplication and generated-playlist exclusions.
These bounds limit retained data; they do not truncate the
search for eligible candidates beyond rejected pages.

Each stream chooses its next song using its own anchor and proposed prefix.
History, artist spacing, genre constraints, feedback, style preference and
exclusions remain part of that decision. Reusing candidate facts must not reuse
a final ranking across different hypotheses or sequence prefixes. Candidate
buffers refill when exclusions consume their useful contents; a rejected first
page is not evidence that the library has no eligible continuation.

The planner also coordinates diversity between streams. Distinct anchors and
continuations are preferred, while small libraries can share songs between
alternatives. A continuation never repeats a recording within its own sequence.
Prepared generation ownership and current eligibility remain authoritative.

## Applying tuning

Apply closes the dialog immediately, retains the requested values and dims the
existing streams while replacements are generated. The current song, player,
heard history and useful existing content remain available. Completion replaces
the streams and restores their normal appearance. Reopening tuning retains the
requested or accepted values.

An explicit Apply is computation the listener requested. It must not be rejected
because generation crosses an arbitrary computation deadline. The browser uses
bounded requests and reconciles status after a missing receipt; it never
automatically repeats a mutation. Worker computation can finish after the
request disconnects. Stop, a newer Apply, a new session or a newer explicit
stream/song choice supersedes obsolete work. Ordinary observation requires
rebasing against current listening state rather than silently losing Apply.

Publish only a complete, current plan. Accepted tuning must agree with the
saved checkpoint before it is confirmed. A partial plan, failed save or missing
response must not manufacture confirmation or overwrite a newer action.
Session steering remains separate from durable taste feedback.

## Performance evidence

Measure a complete Apply, including candidate preparation and all four
continuations. Separate wall time, CPU, candidate-page hydration, profile/vector
loads and scoring. Compare the same captured library, starting state, requested
settings and random seed. Verify each stream's output and constraints alongside
costs. Profiles and run receipts belong in explicit report destinations; this
document describes the architecture rather than individual run history.

`tests/schema/check_radio_tuning_actual.py` is an explicit opt-in comparison,
excluded from normal test collection. It copies a read-only source and its
owned files, fixes the checkpoint, clock and random seed, and checks identical
continuations against the selected local baseline revision. Both variants use
the corrected worker lifetime so the comparison measures complete operations.
Separate runs collect wall/CPU timings and cProfile diagnostics. Fixture-copy
time is setup and is never reported as Apply time.
