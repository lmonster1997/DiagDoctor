#!/usr/bin/env python3
"""
Initialize the DiagDoctor knowledge base.

Creates Qdrant collections and loads initial seed data into the structured KB.

Usage:
    uv run python scripts/init_kb.py
    uv run python scripts/init_kb.py --seed-data doctor/seed_data/initial_knowledge.yaml
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Ensure the doctor package is importable when run as a script
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _SCRIPT_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from src.config import settings  # noqa: E402
from src.knowledge.embeddings import get_embeddings  # noqa: E402
from src.knowledge.struct_kb import StructKnowledgeBase  # noqa: E402
from src.knowledge.vector_kb import VectorKnowledgeBase  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("init_kb")

# Qdrant collection names to pre-create
COLLECTIONS = [
    "historical_cases",
    "code_index",
]


def init_qdrant(qdrant_url: str) -> VectorKnowledgeBase:
    """Initialize Qdrant: create collections if they don't exist."""
    logger.info("Connecting to Qdrant at %s", qdrant_url)

    embeddings = get_embeddings()
    vkb = VectorKnowledgeBase(qdrant_url=qdrant_url, embeddings=embeddings)

    for coll_name in COLLECTIONS:
        vkb.get_collection(coll_name)
        logger.info("  Collection '%s' ready", coll_name)

    return vkb


def init_struct_kb(db_path: str, seed_yaml: str | None = None) -> StructKnowledgeBase:
    """Initialize the structured knowledge base, optionally loading seed data."""
    logger.info("Initializing StructKB at %s", db_path)

    skb = StructKnowledgeBase(db_path=db_path)

    if seed_yaml:
        seed_path = Path(seed_yaml)
        if seed_path.exists():
            counts = skb.bulk_load_from_yaml(seed_path)
            logger.info(
                "  Loaded seed data: %d HTTP codes, %d patterns, %d practices",
                counts["http"],
                counts["patterns"],
                counts["practices"],
            )
        else:
            logger.warning("  Seed data file not found: %s", seed_yaml)
    else:
        logger.info("  No seed data file specified — struct KB is empty")

    return skb


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize DiagDoctor knowledge base")
    parser.add_argument(
        "--qdrant-url",
        default=settings.qdrant_url,
        help=f"Qdrant server URL (default: {settings.qdrant_url})",
    )
    parser.add_argument(
        "--struct-db",
        default="data/struct_kb.db",
        help="Path to SQLite database (default: data/struct_kb.db)",
    )
    parser.add_argument(
        "--seed-data",
        default=None,
        help="Path to YAML seed data file (default: doctor/seed_data/initial_knowledge.yaml)",
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("DiagDoctor Knowledge Base Initialization")
    logger.info("=" * 60)

    try:
        # 1. Initialize Qdrant collections
        vkb = init_qdrant(args.qdrant_url)

        # 2. Initialize structured KB
        skb = init_struct_kb(args.struct_db, args.seed_data)

        # 3. Cleanup
        vkb.close()
        skb.close()

        logger.info("=" * 60)
        logger.info("Knowledge base initialization complete ✓")
        logger.info("=" * 60)

    except Exception as exc:
        logger.error("Initialization failed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
