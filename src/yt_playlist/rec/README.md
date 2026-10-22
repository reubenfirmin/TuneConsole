# Recommendation application boundaries

`home_cards.HomeCards` owns Home candidate assembly, lane and genre policy, pool
selection, offered counts, served impressions and diagnostic audits. Its request
and result dataclasses carry policy inputs and template-independent results. The
job port admits pool work only on an explicit start request; reading cards or
polling a pool does not start work. `web/routes/home.py` parses and validates HTTP
input, renders the results, and preserves the Home audit point after rendering.
Existing rankers, recipes and recommendation surfaces remain the policy engines.

Road-trip work has three owners:

- `road_trip_candidates` acquires provider rows and metadata, reads eligible
  library candidates, and ranks the acquired pool. `InputRequest` and
  `CandidateBatch` describe one provider acquisition without changing a draft.
- `road_trip_mix.construct` accepts a fixed `MixRequest` and explicit random
  source, and returns allocation, ordering and shortfalls in `MixResult`. It has
  no provider, HTTP, repository or playlist-write dependency. Shortfalls remain
  visible while a draft is building and after final fallback fills.
- `road_trip` owns draft creation, incremental acquisition transitions, edits,
  normalization, quotas, rerolls and the persisted JSON shape. It applies mixer
  results to the draft and delegates searches to the acquisition owner.

The road-trip routes continue to persist drafts through `repos.road_trip` and
create playlists through `library.executor`. These boundaries do not change
eligibility, pool budgets, duration fitting or the persisted draft format. Pool
exhaustion behavior, including the concern in #205, remains a separate behavior
change rather than an implicit part of this extraction.
