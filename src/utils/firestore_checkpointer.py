"""
FirestoreCheckpointer — LangGraph checkpoint saver backed by Firestore.

Replaces MemorySaver so agent state survives:
  - Cloud Run scale-to-zero (instance restarts)
  - Server crashes mid-conversation
  - Users taking hours to reply to a Telegram HITL confirmation

Firestore schema:
  langgraph_checkpoints/{thread_id}/checkpoints/{checkpoint_id}
    checkpoint_type: str
    checkpoint_data: bytes
    metadata_type:   str
    metadata_data:   bytes
    parent_checkpoint_id: str | None
    ts: str

  langgraph_checkpoints/{thread_id}/writes/{write_key}
    checkpoint_id: str
    task_id:       str
    channel:       str
    value_type:    str
    value_data:    bytes
"""
import asyncio
from functools import partial
from typing import Any, AsyncIterator, Iterator, Optional, Sequence

from google.cloud import firestore
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from src.utils.logger import get_logger

logger = get_logger(__name__)


class FirestoreCheckpointer(BaseCheckpointSaver):
    """
    LangGraph BaseCheckpointSaver implementation using Firestore as the backend.
    All sync Firestore SDK calls are wrapped in run_in_executor for async safety.
    """

    def __init__(self, db: firestore.Client, collection: str = "langgraph_checkpoints"):
        super().__init__(serde=JsonPlusSerializer())
        self.db = db
        self.collection = collection

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _run(self, fn, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

    def _checkpoint_col(self, thread_id: str):
        return (
            self.db.collection(self.collection)
            .document(thread_id)
            .collection("checkpoints")
        )

    def _writes_col(self, thread_id: str):
        return (
            self.db.collection(self.collection)
            .document(thread_id)
            .collection("writes")
        )

    def _doc_to_tuple(self, thread_id: str, doc) -> CheckpointTuple:
        data = doc.to_dict()
        checkpoint = self.serde.loads_typed(
            (data["checkpoint_type"], data["checkpoint_data"])
        )
        metadata = self.serde.loads_typed(
            (data["metadata_type"], data["metadata_data"])
        )
        parent_id = data.get("parent_checkpoint_id")
        parent_config = (
            {"configurable": {"thread_id": thread_id, "checkpoint_id": parent_id}}
            if parent_id
            else None
        )
        return CheckpointTuple(
            config={"configurable": {"thread_id": thread_id, "checkpoint_id": doc.id}},
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=None,
        )

    # ── Sync interface (required by BaseCheckpointSaver) ─────────────────────

    def get_tuple(self, config: dict) -> Optional[CheckpointTuple]:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = get_checkpoint_id(config)
        col = self._checkpoint_col(thread_id)

        if checkpoint_id:
            doc = col.document(checkpoint_id).get()
            return self._doc_to_tuple(thread_id, doc) if doc.exists else None

        docs = list(
            col.order_by("ts", direction=firestore.Query.DESCENDING).limit(1).stream()
        )
        return self._doc_to_tuple(thread_id, docs[0]) if docs else None

    def put(
        self,
        config: dict,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict,
    ) -> dict:
        thread_id = config["configurable"]["thread_id"]
        parent_id = get_checkpoint_id(config)
        checkpoint_id = checkpoint["id"]

        c_type, c_data = self.serde.dumps_typed(checkpoint)
        m_type, m_data = self.serde.dumps_typed(metadata)

        self._checkpoint_col(thread_id).document(checkpoint_id).set({
            "checkpoint_type": c_type,
            "checkpoint_data": c_data,
            "metadata_type": m_type,
            "metadata_data": m_data,
            "parent_checkpoint_id": parent_id,
            "ts": checkpoint["ts"],
        })
        logger.debug(f"[Checkpointer] Saved {checkpoint_id} for thread {thread_id}")
        return {"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}}

    def put_writes(
        self,
        config: dict,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = get_checkpoint_id(config) or "latest"
        for channel, value in writes:
            v_type, v_data = self.serde.dumps_typed(value)
            self._writes_col(thread_id).document(
                f"{checkpoint_id}_{task_id}_{channel}"
            ).set({
                "checkpoint_id": checkpoint_id,
                "task_id": task_id,
                "channel": channel,
                "value_type": v_type,
                "value_data": v_data,
            })

    def list(
        self,
        config: Optional[dict],
        *,
        filter: Optional[dict] = None,
        before: Optional[dict] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        if not config:
            return
        thread_id = config["configurable"]["thread_id"]
        query = self._checkpoint_col(thread_id).order_by(
            "ts", direction=firestore.Query.DESCENDING
        )
        if limit:
            query = query.limit(limit)
        for doc in query.stream():
            yield self._doc_to_tuple(thread_id, doc)

    # ── Async interface (used by graph.ainvoke) ───────────────────────────────

    async def aget_tuple(self, config: dict) -> Optional[CheckpointTuple]:
        return await self._run(self.get_tuple, config)

    async def aput(
        self,
        config: dict,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict,
    ) -> dict:
        return await self._run(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: dict,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await self._run(self.put_writes, config, writes, task_id, task_path)

    async def alist(
        self,
        config: Optional[dict],
        *,
        filter: Optional[dict] = None,
        before: Optional[dict] = None,
        limit: Optional[int] = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if not config:
            return

        thread_id = config["configurable"]["thread_id"]

        def _fetch():
            query = self._checkpoint_col(thread_id).order_by(
                "ts", direction=firestore.Query.DESCENDING
            )
            if limit:
                query = query.limit(limit)
            return [self._doc_to_tuple(thread_id, doc) for doc in query.stream()]

        for t in await self._run(_fetch):
            yield t
