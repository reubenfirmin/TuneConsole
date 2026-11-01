Parent: #530. Priority: **P2**.

Export the fixed equal PMEmo/DEAM Cruise candidate as a compact, versioned
per-vibe artifact. The current product loader accepts older formats with a
256 KiB limit; the full six-scorer research export is about 3.87 MB. A research
JSON export alone is not a compatible production model.

First implement and check the proposed contract under `experiments/` through
`./experiments/run.py`. Keep the existing audio encoder and all production
coefficients unchanged during this phase.

- Include only required native heads, their fixed mappings, source selections,
  feature order, upstream embedding signature, source plan/model hashes and
  model identity. Freeze the combination and aggregation policy.
- Validate finite coefficients, bounded dimensions/model size, feature and
  taxonomy compatibility, supported learner kinds and safe failure behavior.
- Reproduce source predictions and declared mean-native-before-mapping scores
  on all frozen pilot intervals. Also exercise realistic one- and three-excerpt
  inputs, malformed artifacts and incompatible measurements.
- Report serialized size, load time and scoring costs separately from encoder
  costs. Any compression or compact replacement must retain measured numerical
  or quality equivalence; do not silently substitute another selected model.

Acceptance: a small explicit artifact and strict loader prototype with independent
replay, provenance, resource measurements and focused checks. Document the later
production integration locations and rollback format. No default enablement.
