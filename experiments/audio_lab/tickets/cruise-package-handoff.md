Cruise-only compact artifact and routing prototype are ready for independent
review under the agreed experiments-first scope. Product defaults are unchanged.

Implementation: `experiments/audio_lab_tools/cruise_specialist.py`,
`cruise_export.py`, `test_cruise_specialist.py`; commands use
`./experiments/run.py cruise-package`. The full integration map and remaining
gates are in `experiments/audio_lab/CRUISE_PRODUCTIONIZATION.md`.

The fixed equal PMEmo/DEAM Cruise ensemble exports at **187,217 bytes**, preserving
the exact native coefficients, feature order, extractor signature and declared
mean-native-before-mapping aggregation. Bounded pure-Python loading checks
taxonomy, supported learners/dimensions, finite coefficients, positive scales,
compressed-payload checksum and expansion/trailing-data limits.

Routing has `off`, `shadow` and `active` modes. Active replaces only selected-vibe
Cruise suitability. The other five scores and shared overall classification
remain exact shared-model outputs. Scores are not normalized across mixed scales.
Optional Cruise failure retains the whole shared result; incompatible actual
excerpt provenance is rejected. This is a prototype API, not product activation.

Observed checks on this checkout/runtime:

- `./experiments/run.py cruise-package export --directory experiments/audio_lab/local/mapped-vibes-20261029/cruise-package-2`: passed. All **346 pilot recordings / 1,227 excerpts** replay the research candidate, maximum absolute error **2.22e-16**. Zero changed non-Cruise scores or shared overall choices.
- Real frozen capture envelopes: **93 playback groups**, replaying one excerpt and all available excerpts (**186 cases**). Shared scores exactly equal the real product module; zero changed other-vibe scores. No human-label outcomes or stored predictions scored.
- Approximately **6.2 ms** cold loading, **4.6 ms** median / **8.4 ms** maximum routing on cached pilot features. Encoder/startup costs are outside these timings.
- `./experiments/run.py cruise-package verify --directory experiments/audio_lab/local/mapped-vibes-20261029/cruise-package-2`: passed; original `cruise-package-1` also verified and preserved.
- `./experiments/run.py check`: **116 passed in 3.29 seconds**, using the normal machine test lock. These include 23 Cruise contract cases added to the previous 93 checks. A subsequent run adding two baseline-specific tests waited for the browser-test owner and exited busy before collection; that does not invalidate the completed 116-case run.
- Research syntax parsing and `git diff --check -- experiments`: passed.

No production source/model replacement, app startup/consumer acceptance, release
permission or listening acceptance is claimed. #570 compares refreshed shared
controls; #572–574 retain score, propagation and rollout requirements. Only the
review agent should accept this ticket as Done.
