# Migrations

Routine schema changes go through `python -m src.migrate`, which is additive
and idempotent: it creates missing tables and indexes and touches nothing else.
That runs automatically in the Nixpacks build phase, so a deploy is enough.

**Destructive changes do not go through that path.** Dropping or altering a
column on `fundamentals`, `universe`, or `daily_scores` destroys point-in-time
history, and point-in-time history is the only thing that makes a backtest
honest. Those changes get a hand-written Alembic revision in `migrations/versions/`,
reviewed like any other code.

## Adding a destructive revision

```bash
alembic revision -m "describe the change"
# edit the generated file in migrations/versions/
alembic upgrade head
```

Before writing one, ask whether the data can be preserved instead. A new
nullable column is nearly always better than reshaping an existing one — the
PIT accessors already treat missing values as missing rather than imputing
them, so a partially-populated column is safe to introduce.

## Rebuilding history

If historical data is lost, `python -m src.backfill --days 600 --fundamentals`
reconstructs price bars from Polygon grouped-daily and fundamentals from SEC
XBRL as-reported. What it **cannot** reconstruct is the daily `universe`
snapshot: that records which names were tradeable on each past date, including
companies that have since delisted, and no API will tell you after the fact.
Once those snapshots are gone, every backtest over that period is
survivorship-biased.
