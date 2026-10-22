"""Small retained modes for tests that exercise feedback rather than clustering."""


def seed_modes(store, *mode_ids):
    store.modes.replace_modes([
        dict(mode_id=mid,label=f'Mode {mid}',families=[],centroid=[1.0],size=1,rep_keys=[])
        for mid in mode_ids
    ],[],1)
