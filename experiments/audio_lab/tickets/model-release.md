Parent: #530. Priority: **P2**. Shared dependencies: #570, #571 and #572.
Related release/listening gate: #424.

Develop a dedicated Let off steam / Release model using force, aggression and
power supervision. High tempo, high arousal and negative valence are not required
conditions. Keep this head separate from Energize and shared intensity.

Current common-target evidence on 470 soundtrack excerpts: production anger
ranking NDCG 0.6217, refitted Emotify power 0.6502 and Jamendo aggressive 0.6452.
Both candidates have slightly lower top-list lift than production. This is
modest mixed evidence; neither is an accepted production replacement.

- Freeze source-only-selected Emotify power and Jamendo human aggression as
  separate primary candidates. Compare against Soundtracks anger with complete
  source-component exclusions before fitting. Preserve Coimbra's tag-derived
  aggressive model as a weaker negative/alternative control and EMMA power as
  an explicitly excerpt-mismatched supplementary source.
- Run reciprocal or additional held-out anger/aggression/power evaluations where
  the native labels support them. Keep these references distinct; do not pool
  annotation scales or invent Release targets for VA-only datasets.
- Compare bounded embedding and combined-feature heads, frozen production and
  the #570 refreshed baseline on identical external references. Freeze any
  fixed ensemble before target scoring; do not learn weights from target labels.
- Report ranking, calibrated error, top-list suitability, uncertainty and response
  to human energy versus anger. Include low-tempo forceful music and high-energy
  cheerful music as distinction cases in the listening packet.
- Track Jamendo release permissions in #424; a technically good model does not
  bypass that gate. Export and shadow-score independently through #571/#572.

Acceptance: per-source and common-reference results, an explicit Release
artifact, a distinct aggression/activation behavior check, calibration/resource
evidence and a separate promotion decision. Research stays under `experiments/`;
retain production Release when improvement or permitted use is unresolved.
