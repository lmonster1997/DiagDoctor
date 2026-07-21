"""Rebuild the historical_cases collection (C: symptom vector content changed).

C (hybrid refactor) changed what ``build_symptom_passage`` embeds (full
passage -> ``user_report`` only), so existing points' ``symptom`` vectors are
in a stale vector space. ``ensure_collection`` does NOT trigger a rebuild --
the schema (named vectors symptom+root_cause) is unchanged, only the vector
*content* changed. This script force-rebuilds: delete + recreate empty.

The library is 👍-sourced episodic memory; on a dev library this is a cold
re-ingest (design §5.1). Run AFTER deploying the encoding change, then
re-ingest cases via 👍 (or a seed script).

Usage::

    cd doctor/backend && uv run python scripts/rebuild_collection.py
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

# Add doctor/backend to sys.path so `src.*` imports resolve when run as a script.
DOCTOR_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DOCTOR_BACKEND))

from src.memory.long_term.qdrant_client import (  # noqa: E402
    COLLECTION_NAME,
    ensure_collection,
    get_qdrant_client,
)

# Windows GBK console can't encode some chars; force utf-8 stdout.
with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]


async def main() -> None:
    client = await get_qdrant_client()
    # Force rebuild -- schema unchanged but symptom vector CONTENT changed in C,
    # so ensure_collection's schema-match no-op would leave stale vectors.
    await client.delete_collection(COLLECTION_NAME)
    print(f"[ok] deleted collection: {COLLECTION_NAME}")
    # Recreate empty with the correct named-vector schema (symptom + root_cause).
    await ensure_collection()
    print(f"[ok] recreated empty collection: {COLLECTION_NAME}")
    print(
        "[note] library is now empty -- re-ingest cases via upvote (or a seed "
        "script). Old symptom vectors were stale (C changed the passage: "
        "full-passage -> user_report only)."
    )


if __name__ == "__main__":
    asyncio.run(main())
