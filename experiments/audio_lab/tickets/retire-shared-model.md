Standalone follow-up, **Backlog**. This ticket owns its own hierarchy and is outside the #136/#530 closeout scope. Related prior work: #530; product contract: #136; output evaluation: #405/#587. Reuse their evidence without making retirement a prerequisite for their acceptance.

All six vibe suitability scores now have specialist implementations. Retire the old shared six-way model from the product once its remaining consumers have explicit replacements; preserve its exact artifact, inference recipe, source notices and historical comparisons under `experiments/`.

This retirement was not covered by the specialist adoption tickets. Their accepted scope deliberately preserved shared classification, shared Radio intensity and fallback/rollback behavior.

Current dependencies to remove:

- `audio_vibe_model.apply` loads the shared artifact before calling any specialist. A missing/disabled/invalid shared artifact currently prevents specialist inference. Decouple the specialist composition and its invalidation identity.
- The prediction's six-way `ranking`, selected `vibe`/`vibes`, labels, margins and selection method still come from the shared classifier. Trace displayed predictions, API consumers, storage and diagnostics. Define specialist-based display semantics without treating six independently scaled scores as calibrated probabilities or silently normalizing them into a classifier.
- `vibe_preparation` retains shared Energize/Release channels for Radio intensity; `musical_character` consumes those channels, including inherited contributor lineage. Define and validate a replacement intensity policy across Chill/midpoint/Full on, including slow forceful music and unknown evidence. Raw specialist scores cannot be substituted without evaluating their different scales.
- Missing/invalid/off/incompatible specialists fall back to shared scores. Preserve usable metadata-based output and explicit unknown/partial-capture behavior after retirement. Define rollback without keeping an undeclared runtime dependency on the archived model.
- #587's retained-account audit also found 41 complete legacy captures without usable embeddings, alongside 83 complete recordings using all six specialists. Legacy captures bypass the shared learned model and use the audio heuristic. In the combined 48-slot Mix outputs, all 25 direct-audio Release slots used that legacy heuristic; Relax and Focus likewise selected no directly specialist-scored songs. Explicitly define comparable selection behavior across specialist, provider, inherited, legacy-heuristic and unknown scores. Missing embeddings must not acquire an accidental ranking advantage merely because a fallback uses a different scale. These are routing/coverage observations, not evidence that the selected songs are unsuitable or that the adopted specialists should be rolled back. See `experiments/audio_lab/OUTPUT_EVALUATION_RESULTS.md` and its exact inference audit.
- Preserve existing measurement history, recording identity, source/terms provenance and retained served-profile explanations. Invalidate and rebuild only derived state through bounded background preparation; no live-database test or bulk conversion in a normal migration.

Acceptance:

1. Inventory and remove every product/runtime/package/config dependency on the shared artifact and its retired inference paths. Preserve research replay in experiments; the installed wheel must work with experiments absent and networking disabled.
2. Prove all six specialists run with the old artifact absent, including model changes, fallback, partial/missing captures and restart. Keep original notices and uncertainty statements.
3. Evaluate the replacement intensity and display behavior with existing justified labels and paired complete consumer outputs. Reuse #587 where identities/protocol match; record additional changes separately. No new manual-listening or shadow-rollout gate.
4. Verify bounded preparation, preserved current playback, explicit restrictions and historical provenance on isolated actual-library inputs. Review the rendered predictions/controls affected by the change.
5. Remove obsolete production tests/configuration only after their required guarantees have replacement coverage. Retain the historical baseline under experiments rather than rewriting past results.

This ticket tracks implementation and acceptance of retirement; creating it does not declare the current model unused or authorize silently changing the intensity contract in #624/#587.
