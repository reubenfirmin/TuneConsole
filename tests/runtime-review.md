# Test runtime review: #534

Implementation progress and disposition of the recommendations in issue #534. The source
review described an older snapshot; this ledger reconciles it with the current
shared checkout. This records focused implementation evidence, not independent
acceptance or a claim that the current test suite passes.

## Prioritized recommendations

| Item | Disposition and preserved contract |
| --- | --- |
| P1. Successful fake writes | Seed real fake catalog/readback state in duration, generated-playlist, cluster-save, account-selection, fresh-enrichment and mode-engagement fixtures. `tests/playlist_fixture_support.py` shares catalog construction. Incomplete delivery, retry and acknowledgment failure fixtures retain their original behavior; production retry logic is unchanged. |
| P2. Historical schema setup | `core/test_current_schema.py` builds genuine predecessor templates with SQLite backup and computes the fresh contract once. Each upgrade, idempotence, foreign-key and integrity check still has its own clone. Relationship tests clone a genuine pristine v24 template and explicitly replay historical upgrades before ordinary admission. Their fourteen invalid-write checks share one v25 upgrade, retaining every rejection and rolling back even unexpectedly accepted writes between cases. This tests the actual historical constraints rather than retired read-only views. Populated evidence, nonnumeric ordering, current history/feedback, reopen idempotence and interrupted-upgrade rollback retain separate real upgrade paths. Capture-evidence and recording-vibes upgrade coverage remains intact. |
| P3. Radio motion | Transfer dense fork-lag and fixed-control clearance checks into the stronger motion journey, then remove the older 75-frame duplicate. Preserve broad pure geometry sampling, all four shapes, reversal, desktop/mobile behavior and motion under reduced-motion preferences. The third 1792-by-1398 case retains layout, covers, history navigation and scrolling while omitting the seven repeated motion traversals. Jump the hidden 30-second phase; retain real intervening frames in the 20-second evolution assertion. Diagnostic captures are opt-in. |
| P4. Prepared Home fixtures | `home_fixture_support.publish_menu` publishes four valid cards with sixteen real catalog subjects through the production cache writer. Regeneration failures, tuning geometry, discovery/story exposure and Home markup consumers use this instead of ranking/preparing a library. Analytics retains its real 84-day facts but no longer prepares unrelated Home recommendations. Baseline/tuned eligibility uses two disjoint sixteen-subject publications. Real ranking, publication races, readiness, first-user progression and vibe persistence tests retain their actual pipeline. Post-unlock control tests explicitly isolate the growth gate; growth tests do not use that override. No mutable Store is shared. |
| P5. Similar dialog | Merge 24 journeys into 12, preserving both parents, three widths and artwork states. Bounds, image recovery, checkbox selection, action reachability, scrolling, overflow and Escape remain. Reachability scrolls the actual last rendered action rather than assuming seed order equals display order. |
| P6. Regeneration failure matrix | Run all six failures once with a compact published menu, reading the actual button endpoint rather than rewriting `hx-post`. Two short HTTP/template cases render both actual variants and verify endpoints, targets, swap/trigger attributes, shared handler and held opportunity. Preserve network failure, HTTP error with valid content, 422, missing/invalid fragments and empty success; each still checks deduplication, retry, original rows and animation cleanup. The real held-opportunity/animated-swap integration remains separate. |
| P7. Deterministic waits | Replace slow playlist deletion sleeps with entered/release Events and release in cleanup. Discovery/story clocks wait for actual native observer registration or a qualified dwell timer before advancing; real requests and persisted counts still establish hidden, visible and fragment-poll behavior. The response barrier matches the requested tile's subject/kind/ID so another visible tile's concurrent POST cannot release it early. Analytics likewise waits for its actual Alpine observer/timer. The retained audio-card test runs every normal page tick under a clock, records request times in that clock and preserves retry-spacing/no-retry assertions; recovery waits for the real server response. This replaces 24.4 seconds of exposure waits and eleven seconds of audio retry waits, not a measured wall-clock speedup. The story test also waits for real lazy-fragment completion. Actual extension/media timing remains where page clocks do not control the isolated world or native capture. |
| P8. Fair queue | Keep one compact full drain with unique small-lane survival, blocked exclusions and exhaustion. Preserve partial-consumption, ties, muting, suppression and trace tests. Move the repeated 8,000/32,000 print-only timing comparison to `experiments/investigations/bench_fair_queue.py`. |
| P9. Policy retention | Ordinary preparation retention uses eight generations, four variants and 256-byte evidence while crossing a patched grace period; actual typed publication, bounded owner cleanup and exact latest configuration remain. `bench_policy_retention.py` retains the original 80-generation, 1,000-variant production-default budget on a real file database. All variants are installed through preparation, with one representative candidate pool: policy storage scale is the contract, not a thousand artificial pool publications per generation. The first generation verifies every variant was installed. The original 7 MiB default bound passes. The 1,008-variant sharing check includes canonical file wrappers in its explicit 512 KB ceiling, versus roughly 48 MB unshared. Both legacy byte-limited/resumable cleanup cases use genuine v69 SQL owners, not retired table names on the current schema, and pass with small fixtures. Other untouched legacy policy tests still need reconciliation. |
| P10. Source parsing | Cache immutable source ASTs for architecture traversal and ownership checks; synthetic per-source AST cases remain fresh. Cache parsed release-workflow YAML by file name; all thirty-nine workflow checks pass. No architecture assertion was removed. Three architecture guards failed in the recorded baseline and still failed after caching. |
| P11. Held-out fits | Extract `outer_fold` into the real robust-training implementation and use that same helper for robust and semisupervised before/after isolation checks. Real fitting, normalization, selection and unlabeled-cohort checks remain. Keep complete nested pipeline/export tests. Retain gold/silver full-cohort fitting because its assertions span teacher eligibility across folds and final export. Malformed-artifact tests already construct an artifact directly in the current snapshot; no training removal is needed there. CLI invocation/immutability coverage remains. |
| P12. Held writers | Combine twenty reader scenarios into four real file-backed writer states. Run all five foreground readers while each writer is held, retain private-reader closure checks and post-commit/rollback observations. No mock replaces contention. |
| P13. Pure JavaScript | Move all thirteen sync outcome, transport-recovery and disposal scenarios to `test_sync_outcomes_js.py`, loading actual `home.js` in a fresh Node VM per case. Keep native browser sync/SSE integrations. |
| P14. Data scale | Discovery cleanup uses four compact operation/staging cases plus two retained 8,000-track direct/staged cases. Recent-evidence membership fixtures use three old observations and real catalog/embedding owners while keeping all eight consumer/decode guards. Unrelated churn falls from 1,000/4,000 writes to four, with exact dependency/scoped-capture assertions retained. Retain policy-upgrade volume because the shared genuine v69 harness also proves physical shrink, WAL restoration and resumed compaction; splitting its smaller normalization/early-deferral seeds remains a follow-up during ongoing storage-harness changes. Five Home-refill eligibility cases now use thirty-two subjects total while retaining fresh/held offer capture, source selection, blocked counts and exact generation/context checks. |
| P15. Model dispatch/provenance | Item2vec dispatch uses valid fixed-vector spies while checking actual selection/publication. Five generated-history provenance cases use a fixed published vector space; numerical recall, temporal evaluation and real fit checks retain training. The older duplicate five-fold content-projection report is absent from the current snapshot; distinct leakage/recall integrations remain. |
| P16. Responsive transactions | Keep one 1280-pixel native MV3/card pause-resume journey and one identity typing/help journey. Retain Home-vibe widths: the current slow-request test checks feedback in the viewport, including a short 320-by-640 window. Retain now-playing parents/widths because they exercise different dock parents, source-return wiring and popup/breakpoint geometry. All twelve similar-dialog combinations remain. |
| P17. Resource ownership | Close root/local Store fixtures with yield/finally; bound server startup/join cleanup in consolidated browser fixtures. Bridge readiness uses an Event and its loop/thread is stopped, joined and closed. Extension prompt-lock readiness uses one asynchronous `wait_for_function` instead of repeated browser roundtrips. Actual loop/thread and test-lock regressions remain. Other HTTPServer polling intervals are retained because cancellation/shutdown timing is part of their integration behavior; no global lock changes were made. |

## Concrete consolidations

| Item | Retained coverage |
| --- | --- |
| D1. Merge browser | Initial 2/2 and changed 1/2 assertions join the stronger apply/return journey. Delivery failure and both return destinations remain. |
| D2. Playlist hearts | Remove two mock-only cases; retain six real backend cases with desired-state payloads, sidebar controls, stale sensor handling and reload persistence. |
| D3. Taste recomputation | One six-to-seven dimension transition checks centroid shape, space stamp, stable IDs and no retired modes. |
| D4. Generated duration | Six default source/materialization combinations plus representative lower/upper custom boundaries replace the independent minute cross-product. Explicit unknown duration, lookup behavior, source precedence, receipts, lookup failure and all-overlong rejection remain. |
| D5. Upload album | One response checks availability, ownership/no Save, columns, enrichment, escaping, navigation and upload playback handoff. Partial-library, missing-artist and cover cases stay separate. |
| D6. Album page | Head tools join the render case; exact full-album share URLs and attachment checks join the live-full-album versus incidental-subset case. |
| D7. Cleanup routes | Detail/editor use one response. Cached-summary deletion checks remote effect, empty body and removed row. Strong three-copy keep-one retains refresh and survivor assertions. Negative live-read, backup and Undo cases remain. |
| D8. Home routes | Combine baseline shell/status/feed checks; fingerprint assertions join hidden-until-genres; playlist HTTP status joins the cross-page player case; breadth clamp is a second POST in the roundtrip. Presync and prepared paths remain explicit. Current route/growth failures remain visible. |
| D9. Onboarding | Thin Home and hidden cards/feed share a GET. Common welcome/invalid input runs once; all three formats retain readiness rejection, persistence and destination checks. |
| D10. Empty PPR modes | Keep the empty-mode result in `test_ppr.py`; remove its weaker duplicate from mode rankings. |
| D11. Breadth | Explicit neutral setup joins the stronger rare-family ranking case. |
| D12. Road Trip | The same forty-percent-rock recipe now checks both size preservation and proportion in one case. |
| D13. Mode cards | One sequence checks baseline, generated membership, unaffected items and exclusion changes. Current mute/invalidation remains separate. Fixtures publish through the current bundle owner. |
| D14. Playlist promotion | One success checks remote generated-marker clearing and local generated-group removal. Remote edit failure stays separate. |
| D15. Nags | Last.fm href joins render/dismiss/persistence. Takeout href and initial welcome/no-nag assertions join stronger response cases. |
| D16. Release promotion | Retain authenticated two-page success plus outage; remove the weaker second-page-only duplicate. |
| D17. Repository roundtrips | Keep exact generated-provenance key-set exclusions. Move unique binding fields into stronger identities, tracks, actions, genres, history, playlists, collection and charts roundtrips before deleting their weaker binding-only tests. |
| D18. Analysis | Near-group detection and dismissal share a graph; identical-group ignore joins membership-change reappearance. |
| D19. Waterfall | Both audit rows join the provider-precedence case. |
| D20. Trends | The promotion regression now actually marks/adopts a generated playlist and clears its group before checking catalog artist counts. |

## Other targeted improvements

| Recommendation | Disposition |
| --- | --- |
| Provider validator fixtures | Retain provider transport fixtures using Store: they also exercise durable credentials/cache/observation behavior. Pure validators should remain Store-free; splitting the mixed transport integrations further needs a separate fixture boundary rather than removing their persistence assertions. |
| Spotify eviction | Patch cache capacity to eight and retain eviction/order assertions. |
| Sync truncation | Twenty-six playlists, one with 101 songs, retain both greater-than-25 and greater-than-100 boundaries without building thirty large playlists. |
| Repeated statistical samples | Reuse each sampled result for mode-bandit and transient-recipe assertions. Statistical sample counts are unchanged. |
| Experiment ledger | Retain mature-cohort integration and its negative assignment/probability matrix. These cases prove the report stays powered with 99 controlled and one uncontrolled episode and then rejects promotion. A pure admission stub would lose that connection. Cloning needs a baseline that includes the current policy/ledger owners; the independent-sample requirement was not lowered. |
| Feature assets | Retain the current browser matrix: it verifies loaded execution order, initialized Alpine factories, page errors, responsive overflow and distinct player dock parents. HTTP asset-list checks alone would not establish these contracts. Moving only width-independent inclusion checks remains optional future work. |
| Diagnostic screenshots | Home, discovery, stories, analytics, genre badges, adaptive groups and Radio use `tests.visual_support.capture`, enabled by `TUNECONSOLE_VISUAL_REVIEW=1`. Actual pixel assertions and browser failure artifacts retain capture. |
| Node process startup | Retain fresh VM/process cases and per-case failure reports. No process-startup bottleneck was measured, so ownership/mute/deadline permutations were not batched away. |
| Musical preparation | Normal regression uses five recordings for fanout/index behavior. `bench_musical_preparation.py` retains all 96 recordings, the 15-second throughput budget and indexed query contract. |

## All 25 original benchmark modules

Moved files retain their original base names under
`experiments/investigations/`. They were already opt-in, so relocation itself
does not reduce ordinary pytest discovery time.

| Original module | Disposition |
| --- | --- |
| `bench_audio_lab_model.py` | Move historical runtime/CSP controls, CPU/extractor comparisons and natural reference-window studies. Product runtime digest/CSP checks remain in audio-vibes browser/model tests; real cancellation remains in audio-preview/probe tests. |
| `bench_cleanup_analysis.py` | Move old-git-implementation comparison. |
| `bench_database.py` | Move SQL profiling and old-query comparisons. |
| `bench_enrichment_queue.py` | Move priority SQL profiling and diagnostics. |
| `bench_getsongbpm_pilot.py` | Move bounded live-provider/manual matching pilot. |
| `bench_home_database.py` | Move saved-library SQL timing/plan report. |
| `bench_maintenance_logging.py` | Move real-clock log audit/model-fitting investigation; ordinary fake-clock logging regressions remain. |
| `bench_musical_character.py` | Move saved-catalog listening/audit/retune reports. Update audio-lab imports and runtime digest inputs. |
| `bench_musical_provider_pilot.py` | Move live ReccoBeats/manual quality pilot. Neutral matching helpers live in `tests/musical_provider_support.py`; ordinary provider matching imports that helper. |
| `bench_ready_recovery.py` | Move incident-specific saved-snapshot rehearsal. Recurring protected-graph recovery cycles remain in `bench_evidence_recovery.py`, and durable restore remains in `bench_choice_retention.py`. |
| `bench_taste_groups.py` | Split: synthetic engine/feature comparisons move; production preparation/update budgets, membership, lineage and retained definitions remain opt-in in tests. Shared input/report helpers live in `tests/group_benchmark_support.py`. |
| `bench_svd_background.py` | Split: two saved-library before/after comparisons move; four real full-SVD/generation/refresh foreground checks remain opt-in in tests. |
| `bench_analytics_reads.py` | Retain output parity and foreground latency acceptance. |
| `bench_choice_evaluation.py` | Retain real prepared/shared data, fit and concurrent reads. |
| `bench_choice_retention.py` | Retain prolonged retention and durable restore. |
| `bench_evidence_recovery.py` | Retain quota pressure and repeated recovery cycles. |
| `bench_group_home.py` | Retain real membership/ranking/resource acceptance. |
| `bench_home_prepared.py` | Retain incremental preparation, real menus and exposure. |
| `bench_progressive_preparation.py` | Retain bounded preparation during ongoing writes. |
| `bench_progressive_recovery.py` | Retain cold/growing-library usefulness and resources. |
| `bench_public_flatpak.py` | Retain signed/public packaged release acceptance. |
| `bench_rec_sparse.py` | Retain sparse-model memory and no-dense-fallback acceptance. |
| `bench_recording_vibes.py` | Retain genuine historical evidence-preserving upgrades. |
| `bench_scorecard_attribution.py` | Retain real batches/index/private-reader acceptance. |
| `bench_vibe_predictions.py` | Retain cold/upgrading prediction usefulness and budgets. |

The new recurring policy-retention and musical-preparation budget files remain
in `tests/`; the new print-only fair-queue study lives with investigations.
`experiments/run.py investigate <file> ...` explicitly loads shared pytest
fixtures, validates the investigation path and does not acquire the global-suite
lock. Documentation and frozen runtime source/digest lists follow moved modules.

## Targeted validation and limits

Environment: Python 3.14.8, SQLite 3.50.2, shared changing checkout and concurrent
machine activity. Only explicit targeted pytest paths were run. No full suite,
commit, push or native user-database migration was performed for this ticket.
Pytest uses the allowlisted plain `uv run pytest <paths>` form with no environment
prefix or permission override. Browser cases additionally select `-m browser`.

Reports are `/tmp/tuneconsole-534-<name>.xml`, outside disposable basetemp trees.
Each row is a separate run; counts must not be added as independent coverage.

| Report | Result and meaning |
| --- | --- |
| `schema-after` | 111 passed in 636.84s: genuine predecessor contracts, integrity and idempotence at that checkout state. Later admission/history changes require renewed verification. |
| `similar-rendered-order` | 12 passed in 71.33s: all retained dialog combinations. |
| `browser-after` | 23 passed, 2 setup errors, 47 deselected in 158.78s. Merge, real-backend hearts and Event-held deletion passed; the dialog scroll setup errors were fixed and superseded by the twelve-case run above. |
| `radio-locks` | 5 passed, 14 deselected in 79.76s: stronger 1440/390 motion journeys and three extension lock cases. |
| `album-js` | 31 passed, 5 failed in 77.58s. All thirteen Node scenarios and consolidated upload render passed. Five album readiness checks returned 409 rather than 200. |
| `render-delete` | 28 passed, 104 deselected in 55.48s: selected Node, album/upload, pagination, provenance and cleanup cases. |
| `tail-network` | 151 passed, 4 failed in 448.38s. Two mode fixtures and the truncation fixture were corrected and passed later; first-sync expectation of a nonempty state remains unresolved. |
| `model-isolation` | 57 passed in 124.25s: robust/semisupervised training, full pipeline/export, mode exclusion, item2vec, roll and bandit checks. |
| `readback-dispatch` | 38 passed, 5 failed, 102 deselected in 192.62s. Readback successes, fixed-vector dispatch/provenance and eight repository roundtrips passed. Three account-selection Road Trip cases require a revision, and two scoped album unsaves returned 422. |
| `split-bounds` | 27 passed, 4 failed, 1 skipped in 302.12s. Both retained large discovery cases and 96-recording throughput passed. Four compact discovery cases hit an accidentally retained large-byte assertion, subsequently corrected. Production taste-group acceptance skipped without its frozen input. |
| `discovery-small-final` | 4 passed, 26 deselected in 21.17s after the compact-byte assertion correction. |
| `native-typing` | 1 passed, 1 failed, 31 deselected in 20.40s. Native pause/resume passed; identity typing expected two valid indicators and found zero. |
| `unit-timing-after` | 35 passed, 4 failed in 88.44s: three architecture guards and final Radio publication ownership fence. The corresponding architecture failures were also recorded before caching. |
| `final-core` | 11 passed, 6 failed, 137 deselected in 171.14s. Current Home welcome text, direct bundle publication, preparation readiness and breadth gate differ from expectations; progressive capture lacks two expected dependencies. The generated-mode identity fixture failure was corrected and superseded by `model-isolation`. |
| `small-bounds` | Initial 5 failures in 18.23s were fixture mismatches. Typed policy publication/sharing, genuine legacy scan owners and real recent-evidence subjects are now covered by the passing focused runs below. |
| `fixture-recheck` | 5 failed, 32 deselected in 1.44s: ordinary admission rejects genuine v24 before relationship assertions execute. Historical template setup is genuine; the explicit upgrade harness needs reconciliation. |
| `engagement-readback` | Initial domain-owner setup rejection is superseded by the three passing engagement cases in `owner-recheck`. Fixtures now use the bundle publication owner. |
| `small-fixtures` | Eight recent-evidence cases passed; this run also contained two initially passing template checks and three engagement setup errors. Later template/engagement results supersede those portions. |
| `owner-recheck` | 9 passed, 31 deselected in 68.18s: five compact baseline/tuned eligibility cases, three real engagement/readback cases and compact preparation retention. The retention helper was subsequently narrowed to policy scale and rechecked below. |
| `regeneration` | 6 passed, 1 deselected in 49.89s: every retained failure, deduplication, cleanup, original-row and successful retry assertion with compact publication. |
| `controls` | 5 passed, 11 deselected in 39.97s: four tuning-launcher viewports plus the dense tab/keyboard/genre/subgenre/era geometry journey. |
| `radio-layout` | 2 passed, 6 deselected in 33.92s: tall history/layout/scrolling and real artwork evolution plus hidden-phase jump. Desktop/mobile motion evidence remains in `radio-locks`. |
| `exposure-recheck` | Four discovery cases passed at both widths; two story/analytics fixture failures were subsequently resolved. |
| `story-clock-recheck` / `story-clock-final` | Analytics exposure passed in the first run; the final story run passed in 9.43s after awaiting actual lazy-fragment completion. Hidden headings, visible dwell and poll deduplication remain asserted. |
| `published-consumers` | 6 passed, 11 deselected in 59.16s: all three reading layouts, both real analytics/filtering widths and sparse-history behavior after removing unrelated Home preparation. |
| `menu-contracts` | 12 passed, 1 failed, 54 deselected in 102.04s: eleven first-use/readiness/progression cases and plain template wiring passed. Modeled wiring exposed seven INSERT placeholders for six `mix_choice_options` columns; the source was subsequently corrected elsewhere in the shared checkout. |
| `markup-current` | Both real template variants passed in 11.25s after that source correction; no assertion was relaxed. |
| `menu-readiness` | 5 passed, 46 deselected in 42.46s: one/three/four-card readiness across Home, growth and setup, plus both unusable-offer cases. Explicit tuned fixture publication preserves existing model context. |
| `policy-compact-final` | 1 passed, 14 deselected in 6.60s: eight preparation cycles, patched grace, bounded retirement, byte/count ceilings and exact latest ranker/decay. |
| `policy-variant-budget` | 1 passed in 51.21s: all 80 generations and 1,000 policy variants per generation, production-default grace and original 7 MiB ceiling. The earlier artificial thousand-pool attempts were interrupted (746.40s in memory; 120.14s on disk), not reported as passes or comparable speed measurements. |
| `policy-scan-bounds` | 2 passed, 13 deselected in 2.53s: genuine SQL-policy predecessor, byte-limited cursor resumption and bounded orphan-body collection. |
| `audio-retry-clock` | 1 passed, 33 deselected in 7.20s: actual failed/successful HTTP loads, all interval ticks, backoff spacing, saved answer restoration and no further loads. |
| `workflow-cache` | All 39 workflow checks passed in 1.59s with parsed YAML reused. |
| `final-fixtures` | 17 passed, 11 deselected in 95.80s on the later shared checkout: both actual markup variants, all recent-evidence cases, engagement/readback and the selected compact retention/sharing/legacy-scan cases. |
| `final-relationships-v2` | All 10 retained tests passed in 69.46s, including all fourteen rejected writes within one rollback-isolated historical constraint matrix. The first draft replayed unrelated later metadata conversions and hit retired-view/row-ID mismatches; this passing run supersedes it and the earlier `fixture-recheck` admission failures. |
| `final-browser` | 9 passed, 1 failed in 90.62s. All six regeneration failures, both discovery widths and retained audio retry passed. Story dwell exposed an imprecise response barrier: an adjacent subject's POST could release it before the target write. The barrier now matches the actual requested subject or discovery kind/ID. |
| `final-guards` | 66 passed, 3 failed in 28.08s. All 13 Node scenarios and 39 workflow checks passed. The same three architecture guards fail in `/tmp/tuneconsole-534-baseline.xml`: import-cycle detection, repository-owner callers, and frozen-numbered-migration runtime imports. Their assertions remain intact. |

Additional focused checks: generated-playlist promotion integration passed
(2 cases, 8.17s); `./experiments/run.py check --syntax-only` parsed 194 files;
the moved taste-group and musical-provider investigations each collected one
case through `./experiments/run.py investigate <file> --collect-only -q`.
The six screenshot-helper browser modules collected all 75 current cases.

Representative exact invocations, with unique basetemp/report paths omitted:

```sh
uv run pytest tests/test_audio_vibe_robust_training.py tests/test_audio_vibe_semisupervised.py tests/test_mode_cards_no_repeat.py tests/test_item2vec.py tests/test_roll_recipe.py tests/test_mode_bandit.py --tb=short --maxfail=5
uv run pytest tests/repos/test_discovery_cleanup_isolation.py -k 'large_cleanup and not 8000' --tb=short --maxfail=5
uv run pytest -m browser tests/browser/test_setup_browser.py tests/browser/test_play_pause_confirmation_browser.py -k 'identity_guide_stays_open or card_confirms_pause_resume' --tb=short
uv run pytest tests/repos/test_relationship_constraints.py tests/repos/test_discovery_cleanup_isolation.py -k 'invalid_direct or populated_upgrade or failed_upgrade or non_numeric or 8]' --tb=short --maxfail=5
uv run pytest tests/test_mode_engagement_integration.py --tb=short --maxfail=1
uv run pytest -m browser tests/browser/test_mix_regeneration_browser.py -k recovers_from_failure --tb=short
uv run pytest -m browser tests/browser/test_into_recently_browser.py -k story_cooldown --tb=short
uv run pytest tests/rec/test_policy_storage.py -k 'legacy_cleanup or orphan_body or repeated_advancing' --tb=short
uv run pytest tests/bench_policy_retention.py --tb=short
uv run pytest tests/test_release_workflows.py --tb=short
./experiments/run.py check --syntax-only
./experiments/run.py investigate bench_taste_groups.py --collect-only -q
./experiments/run.py investigate bench_musical_provider_pilot.py --collect-only -q
```

Timing observations do not establish a controlled speedup. Three matched
predecessor cases totalled 35.347s before and 31.116s after; eleven matched passing
unit cases totalled 30.058s before and 25.076s after. Different selections,
template amortization, concurrent edits and machine activity prevent attributing
those differences solely to this change. A same-revision/environment comparison
of both lanes remains outstanding. Full-suite work remains with the review owner,
subject to the user's current restriction to focused tests. Historical CI weights are not
measurements of these changes.

Required review follow-up: establish which earlier current-storage/Home/typing
failures remain after ongoing integration, and recheck their affected focused
contracts. The relationship fixture admission mismatch is resolved; the broader
predecessor matrix still needs integration verification after storage changes.
Run taste-group production acceptance with its frozen input. The policy budget
is verified against its actual owner. The three baseline architecture failures
remain visible. No current whole-suite status is established.
