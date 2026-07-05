"""One-shot repair of like-ratchet weight inflation.

Root cause (fixed alongside this module): YTM's likeStatus is per-context, so a liked track that
also sat in ordinary playlists read INDIFFERENT there. The sync's last-writer-wins rated map
flapped such tracks LIKE/INDIFFERENT between runs; the INDIFFERENT pass deleted the like row, the
next LIKE pass re-recorded it as "first seen" and re-graduated its facets (no once-ever stamp
existed). Repeated daily, a handful of real likes became dozens of like-source graduations,
pinning artist weights at the GENRE_MAX cap and skewing every surface.

This module removes the re-processing surplus those flaps baked into permanent weights. It runs
once per database from Store.init_schema, stamped via the 'like_ratchet_repaired' settings key
(the stamp stores a summary line for audit). Tests exercise plan_like_ratchet_repair (the dry run)
directly; the live database is only ever touched through the init path.
"""
import logging

from yt_playlist.rec import rec_params, transient

logger = logging.getLogger(__name__)

STAMP_KEY = "like_ratchet_repaired"

# The like graduation weight in force while the surplus accrued. Entitlement is "one graduation
# batch per real like set" AT THAT WEIGHT: the repair removes the re-processing surplus, it does
# not re-litigate the original graduations under the new, lower sync-like weight.
_OLD_SOURCE_W_LIKE = 1.0


def plan_like_ratchet_repair(store, now) -> dict:
    """Dry run: compute the full repair plan without writing anything.

    For each axis with like-source rows in rec_grad_log:
    - entitled: replay the CURRENT like set through the graduation math with a FRESH ledger. Each
      like was applied one key at a time (apply_dislikes iterates the status map key by key), so
      its facet-presence share is 1.0 and every facet it carries receives _OLD_SOURCE_W_LIKE; a
      graduation fires when the running total crosses theme_threshold and discounts it once,
      exactly graduate_facet's mechanic. Per-axis counts are order-independent (every like adds
      the same amount), so replay order cannot change the outcome.
    - surplus = actual like-source log rows minus entitled.
    - weight_after = weight_before / graduate_up ** surplus, floored at 1.0 and at the axis's
      non-like entitlement (the product of its mood/slider/play/vibe graduation factors: those
      graduations remain earned), and never above weight_before.

    Axes with any like-source graduation also get their rec_theme residual zeroed in the apply
    step: the ledger does not attribute accumulated pressure by source, so the like share of a
    polluted axis's pending score cannot be separated out. Zeroing the whole axis is the
    conservative choice: legitimate play/slider pressure re-accrues within days, while a polluted
    crossing would bake another wrong permanent nudge.
    """
    actual, nonlike_floor = {}, {}
    for r in store.graduation_audit_rows():
        if r["source"] == "like":
            actual[r["axis"]] = actual.get(r["axis"], 0) + 1
        else:
            nonlike_floor[r["axis"]] = nonlike_floor.get(r["axis"], 1.0) * r["factor"]
    threshold = rec_params.get_param(store, "theme_threshold")
    graduate_up = rec_params.get_param(store, "graduate_up")
    entitled, ledger = {}, {}
    for key in reversed(store.recent_liked_keys()):        # oldest-first (see docstring: order-free)
        for axis in transient.facets_for(store, [key]):
            score = ledger.get(axis, 0.0) + _OLD_SOURCE_W_LIKE
            if score >= threshold:      # graduate_facet fires at most once per bump: one discount
                entitled[axis] = entitled.get(axis, 0) + 1
                score -= threshold
            ledger[axis] = score
    halflife = rec_params.get_param(store, "weight_revert_halflife_d")
    weights = store.get_weights(now=now, revert_halflife_d=halflife)
    adjustments = []
    for axis in sorted(actual):
        surplus = actual[axis] - entitled.get(axis, 0)
        w_before = weights.get(axis, 1.0)
        target = w_before / (graduate_up ** surplus) if surplus > 0 else w_before
        w_after = min(w_before, max(1.0, nonlike_floor.get(axis, 1.0), target))
        adjustments.append({"axis": axis, "actual": actual[axis],
                            "entitled": entitled.get(axis, 0), "surplus": surplus,
                            "weight_before": w_before, "weight_after": w_after,
                            "theme_before": store.get_theme(axis) or 0.0})
    return {"adjustments": adjustments}


def apply_like_ratchet_repair(store, now):
    """Run the one-shot repair unless this database is already stamped (idempotent). Returns the
    summary line written into the stamp, or None when nothing was run.

    Beyond the weight corrections, two backfills make the surrounding fixes hold on migrated data:
    - provenance: every pre-existing like row is stamped 'sync' (they were all bulk-discovered;
      see RecModelRepo.stamp_sync_like_provenance), keeping them out of the transient model.
    - once-ever graduation stamps: every existing like/dislike key has already graduated (that is
      the very surplus repaired here), so the stamp is armed now; a future clear/re-record cycle
      must not re-graduate them.
    """
    if store.get_setting(STAMP_KEY):
        return None
    plan = plan_like_ratchet_repair(store, now)
    changed = 0
    for adj in plan["adjustments"]:
        if adj["weight_after"] != adj["weight_before"]:
            store.set_weight(adj["axis"], adj["weight_after"], now=now)
            changed += 1
        if adj["theme_before"]:
            store.discount_theme(adj["axis"], adj["theme_before"])   # zero the polluted residual
        logger.info(
            "like-ratchet repair: %s actual=%d entitled=%d surplus=%d weight %.4f -> %.4f "
            "(pending theme %.3f -> 0)", adj["axis"], adj["actual"], adj["entitled"],
            adj["surplus"], adj["weight_before"], adj["weight_after"], adj["theme_before"])
    stamped_prov = store.stamp_sync_like_provenance()
    grad_stamps = 0
    for key in store.recent_liked_keys():
        grad_stamps += 1 if store.mark_graduated_once(key, "like_grad", now) else 0
    for key in store.disliked_identity_keys():
        grad_stamps += 1 if store.mark_graduated_once(key, "dislike_grad", now) else 0
    summary = (f"adjusted {changed} of {len(plan['adjustments'])} like-touched axis weight(s); "
               f"{stamped_prov} like row(s) stamped sync-provenance; "
               f"{grad_stamps} once-ever graduation stamp(s) backfilled")
    store.set_setting(STAMP_KEY, summary)
    logger.info("like-ratchet repair complete: %s", summary)
    return summary
