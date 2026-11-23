# Database maintenance

## Startup and migrations

The oldest supported installed baseline is [v0.1.19](https://github.com/reubenfirmin/TuneConsole/releases/tag/v0.1.19),
published November 12, 2026: numbered schema 107 and regular change 37.
Supported installations receive structural checks and regular SQL migrations.
Fresh databases load the complete compiled schema directly.

Older installations reinitialize their local database atomically and receive a
dismissible early-adopter notice. Saved account bindings and extension pairing
survive the reset so configuration and queued playback keep their account IDs.
YouTube preferences remain on YouTube. An interrupted reset rolls back the
database and notice together. Unknown, malformed or newer migration histories
are rejected without resetting data.

`migrations/regular.py` owns incremental changes and the `schema_changes` ledger.
`migrations/registry.py` retains the numbered history as immutable metadata.
Supported upgrades preserve every existing history row and timestamp. Each new
change and its stamp commit together in the installed database. Some changes
need data preparation in bounded transactions before the schema cutover.

```sh
uv run python -m yt_playlist.core.migrations upgrade-schema /path/to/state.db
uv run python -m yt_playlist.core.migrations audit /path/to/state.db
```

Normal startup applies pending changes before opening the UI. Preparation
hooks save progress in the database so an interrupted upgrade can resume on
the next launch. `upgrade-schema` runs the same changes on a stopped installation.
`audit` checks a committed snapshot read-only.

The compiler applies the same migrations to an empty release schema and
exports `schema/current.json`:

```sh
uv run python scripts/export-current-schema.py
```

## Running app status

`GET /storage-maintenance/status` reports recommendation cleanup,
listening-history cleanup and pending playback feedback.

## Ownership references

- [Schema owners and identity](../../../SCHEMA_OWNERSHIP.md)
- [Field invariants](../../../SCHEMA_INVARIANTS.md)
- [Retained storage contracts](storage-contracts.md)
- [Managed file storage](../../../experiments/retained_copy/FAO-OWNERS.md)
- [Retained-copy experiment history](../../../experiments/retained_copy/README.md)
- [Recommendation publication](../rec/README.md)
