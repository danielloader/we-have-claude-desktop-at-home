"""Elasticsearch-backed ChatStore.

Written against elasticsearch-py 8 async API. Requires the `elastic` extra.
Uses refresh="wait_for" on writes so a list right after a write sees the document,
at the cost of write latency; drop it if throughput matters more than read-your-writes.
"""

from datetime import datetime
from typing import Any

from ..models import Conversation, Message, UsageRecord, UsageSummary, new_id, now

_MAPPINGS: dict[str, dict[str, Any]] = {
    "conversations": {
        "properties": {
            "id": {"type": "keyword"},
            "user_id": {"type": "keyword"},
            "title": {"type": "text"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
        }
    },
    "messages": {
        "properties": {
            "id": {"type": "keyword"},
            "conversation_id": {"type": "keyword"},
            "user_id": {"type": "keyword"},
            "role": {"type": "keyword"},
            "content": {"type": "text"},
            "thinking": {"type": "text"},
            "created_at": {"type": "date"},
        }
    },
    "usage": {
        "properties": {
            "id": {"type": "keyword"},
            "user_id": {"type": "keyword"},
            "conversation_id": {"type": "keyword"},
            "message_id": {"type": "keyword"},
            "provider": {"type": "keyword"},
            "model": {"type": "keyword"},
            "input_tokens": {"type": "long"},
            "output_tokens": {"type": "long"},
            "latency_ms": {"type": "long"},
            "created_at": {"type": "date"},
        }
    },
}


class ElasticChatStore:
    def __init__(self, url: str, *, api_key: str | None = None, index_prefix: str = "chat") -> None:
        from elasticsearch import AsyncElasticsearch  # optional dependency

        self._es = AsyncElasticsearch(url, api_key=api_key)
        self._prefix = index_prefix

    def _idx(self, name: str) -> str:
        return f"{self._prefix}-{name}"

    async def init(self) -> None:
        for name, mapping in _MAPPINGS.items():
            if not await self._es.indices.exists(index=self._idx(name)):
                await self._es.indices.create(index=self._idx(name), mappings=mapping)

    async def close(self) -> None:
        await self._es.close()

    async def create_conversation(self, user_id: str, title: str) -> Conversation:
        ts = now()
        conv = Conversation(id=new_id(), user_id=user_id, title=title, created_at=ts, updated_at=ts)
        await self._es.index(
            index=self._idx("conversations"),
            id=conv.id,
            document=conv.model_dump(mode="json"),
            refresh="wait_for",
        )
        return conv

    async def list_conversations(self, user_id: str) -> list[Conversation]:
        resp = await self._es.search(
            index=self._idx("conversations"),
            query={"term": {"user_id": user_id}},
            sort=[{"updated_at": "desc"}],
            size=500,
        )
        return [Conversation.model_validate(h["_source"]) for h in resp["hits"]["hits"]]

    async def list_all_conversations(self) -> list[Conversation]:
        resp = await self._es.search(
            index=self._idx("conversations"),
            query={"match_all": {}},
            sort=[{"updated_at": "desc"}],
            size=10_000,
        )
        return [Conversation.model_validate(h["_source"]) for h in resp["hits"]["hits"]]

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        from elasticsearch import NotFoundError

        try:
            resp = await self._es.get(index=self._idx("conversations"), id=conversation_id)
        except NotFoundError:
            return None
        return Conversation.model_validate(resp["_source"])

    async def touch_conversation(self, conversation_id: str, *, title: str | None = None) -> None:
        doc: dict[str, Any] = {"updated_at": now().isoformat()}
        if title is not None:
            doc["title"] = title
        await self._es.update(
            index=self._idx("conversations"), id=conversation_id, doc=doc, refresh="wait_for"
        )

    async def delete_conversation(self, conversation_id: str) -> None:
        await self._es.delete_by_query(
            index=self._idx("messages"),
            query={"term": {"conversation_id": conversation_id}},
            refresh=True,
        )
        await self._es.delete(index=self._idx("conversations"), id=conversation_id, refresh="wait_for")

    async def append_message(self, message: Message) -> None:
        await self._es.index(
            index=self._idx("messages"),
            id=message.id,
            document=message.model_dump(mode="json"),
            refresh="wait_for",
        )

    async def list_messages(self, conversation_id: str) -> list[Message]:
        resp = await self._es.search(
            index=self._idx("messages"),
            query={"term": {"conversation_id": conversation_id}},
            sort=[{"created_at": "asc"}],
            size=10_000,
        )
        return [Message.model_validate(h["_source"]) for h in resp["hits"]["hits"]]

    async def record_usage(self, record: UsageRecord) -> None:
        await self._es.index(
            index=self._idx("usage"), id=record.id, document=record.model_dump(mode="json")
        )

    async def usage_summary(self, *, since: datetime | None = None) -> list[UsageSummary]:
        resp = await self._es.search(
            index=self._idx("usage"),
            size=0,
            query=_since_query(since),
            aggs={
                "by_user": {
                    "terms": {"field": "user_id", "size": 10_000},
                    "aggs": {
                        "input_tokens": {"sum": {"field": "input_tokens"}},
                        "output_tokens": {"sum": {"field": "output_tokens"}},
                    },
                }
            },
        )
        return [
            UsageSummary(
                user_id=b["key"],
                messages=b["doc_count"],
                input_tokens=int(b["input_tokens"]["value"]),
                output_tokens=int(b["output_tokens"]["value"]),
            )
            for b in resp["aggregations"]["by_user"]["buckets"]
        ]

    async def usage_for_user(self, user_id: str, *, since: datetime | None = None) -> UsageSummary:
        resp = await self._es.search(
            index=self._idx("usage"),
            size=0,
            query={"bool": {"filter": [{"term": {"user_id": user_id}}, _since_query(since)]}},
            aggs={
                "input_tokens": {"sum": {"field": "input_tokens"}},
                "output_tokens": {"sum": {"field": "output_tokens"}},
            },
        )
        total = resp["hits"]["total"]["value"]
        return UsageSummary(
            user_id=user_id,
            messages=total,
            input_tokens=int(resp["aggregations"]["input_tokens"]["value"]),
            output_tokens=int(resp["aggregations"]["output_tokens"]["value"]),
        )


def _since_query(since: datetime | None) -> dict[str, Any]:
    if since is None:
        return {"match_all": {}}
    return {"range": {"created_at": {"gte": since.isoformat()}}}
