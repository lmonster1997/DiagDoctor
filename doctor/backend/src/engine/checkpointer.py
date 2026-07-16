"""Persistent checkpointer for the DiagDoctor graph.

The diagnosis graph is compiled at **module-load time** (sync) -- ``main.py``
calls ``mount_copilotkit`` -> ``get_copilotkit_graph()`` before the event loop
is running. A persistent ``AsyncSqliteSaver`` cannot be constructed there
because its ``__init__`` calls ``asyncio.get_running_loop()`` (no loop yet at
import time), and the sync ``SqliteSaver`` does not implement the async
checkpoint methods the graph needs (``aget_tuple`` raises
``NotImplementedError``).

``_LazyAsyncSqliteSaver`` bridges that: it is a ``BaseCheckpointSaver``
subclass that is constructed sync (stores only the conn string) and
materialises a real ``AsyncSqliteSaver`` on the **running** loop the first time
any async method is called -- which is always inside a request (uvicorn's
single loop). The async context manager is kept alive for the saver's lifetime
so the underlying aiosqlite connection is not closed between requests.

Persistence target: ``settings.checkpoint_db_path`` (``data/checkpoints.db``).
This is the foundation for #5 HITL (``interrupt()`` + resume from a checkpoint
that survives process restarts). See ``docs/followup-plan-20260715.md`` #7.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.runnables.config import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from src.config import settings
from src.observability.logger import get_logger

logger = get_logger(__name__)


class _LazyAsyncSqliteSaver(BaseCheckpointSaver):  # type: ignore[type-arg]
    """Sync-constructible proxy that lazily materialises an ``AsyncSqliteSaver``.

    See module docstring for the sync-build / async-saver mismatch this resolves.
    """

    def __init__(self, conn_string: str) -> None:
        # JsonPlusSerializer matches AsyncSqliteSaver.from_conn_string's default
        # serde, so checkpoint blobs round-trip identically to the stock saver.
        super().__init__(serde=JsonPlusSerializer())
        self._conn_string = conn_string
        self._saver: AsyncSqliteSaver | None = None
        # Keep the async context manager alive so the aiosqlite connection is
        # not closed after the first request.
        self._cm: Any = None

    async def _materialize(self) -> AsyncSqliteSaver:
        if self._saver is None:
            parent = os.path.dirname(self._conn_string)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # from_conn_string returns an async context manager whose
            # __aenter__ opens the aiosqlite connection + runs setup() (creates
            # the checkpoint tables). Entering it manually keeps it alive.
            self._cm = AsyncSqliteSaver.from_conn_string(self._conn_string)
            self._saver = await self._cm.__aenter__()
            logger.info("sqlite_checkpointer_materialized", path=self._conn_string)
        return self._saver

    # ── async methods (used by the async graph: ainvoke / astream / aget_state) ──

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await (await self._materialize()).aget_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        saver = await self._materialize()
        async for item in saver.alist(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await (await self._materialize()).aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await (await self._materialize()).aput_writes(config, writes, task_id, task_path)

    # ── sync methods: not supported (the graph is async-only). Declared so the
    #     class is concrete; never called on the async code path. ──────────────

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        raise NotImplementedError("LazySqliteSaver is async-only; use aget_tuple.")

    def list(  # noqa: A003 - matches BaseCheckpointSaver.list
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        raise NotImplementedError("LazySqliteSaver is async-only; use alist.")

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        raise NotImplementedError("LazySqliteSaver is async-only; use aput.")

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        raise NotImplementedError("LazySqliteSaver is async-only; use aput_writes.")


def make_checkpointer() -> _LazyAsyncSqliteSaver:
    """Build the lazy sqlite checkpointer from settings.checkpoint_db_path."""
    return _LazyAsyncSqliteSaver(settings.checkpoint_db_path)
