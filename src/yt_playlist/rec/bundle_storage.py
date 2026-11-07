"""Offline connection adapter for historical bundle preparation callers."""
from yt_playlist.core.bundle_plan import (
    EXPLANATION_BYTES, ITEM_FIELDS, LANES, REUSE_GENERATIONS, publication,
)
from yt_playlist.controllers.recommendation_publications import prepare_bundle
from yt_playlist.repos.bundle_storage import BundleInputs


def prepare(conn, bundle, fresh, *, reuse_conn=None, **options):
    previous = BundleInputs(reuse_conn) if reuse_conn is not None else None
    return prepare_bundle(BundleInputs(conn), bundle, fresh, previous=previous, **options)
