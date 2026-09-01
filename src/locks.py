"""Process-wide locks shared between the API and the auto-updater.

They live here, in a module with no dependencies, so that both `src.api` and
`src.scheduler` can hold the SAME lock without importing each other. The point
of sharing is single-flighting: a scheduled bar refresh and someone tapping
Backfill on /admin must not run at the same time against the same tables.
"""

from __future__ import annotations

import asyncio

# Held by every data load, scheduled or manual.
BACKFILL = asyncio.Lock()
