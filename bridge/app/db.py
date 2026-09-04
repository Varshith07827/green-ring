"""MongoDB persistence for every message that flows through the bridge.

One collection (`messages` by default) holds both directions. Documents are
keyed on (sessionId, messageId) and written with upserts, so the record created
by POST /send and the later `message.sent` / `message.ack` webhooks for the same
WhatsApp message collapse into a single row instead of racing each other.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, ReturnDocument
from pymongo.errors import PyMongoError

log = logging.getLogger("bridge.db")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, uri: str, db_name: str, collection: str, events_collection: str):
        self._uri = uri
        self._db_name = db_name
        self._collection_name = collection
        self._events_name = events_collection
        self.client: AsyncMongoClient | None = None

    async def connect(self) -> None:
        self.client = AsyncMongoClient(
            self._uri,
            serverSelectionTimeoutMS=8000,
            tz_aware=True,
            appname="openwa-bridge",
        )
        await self.client.admin.command("ping")
        await self._ensure_indexes()
        log.info("mongodb connected: db=%s collection=%s", self._db_name, self._collection_name)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None

    @property
    def messages(self):
        if self.client is None:
            raise RuntimeError("mongodb is not connected")
        return self.client[self._db_name][self._collection_name]

    @property
    def events(self):
        if self.client is None:
            raise RuntimeError("mongodb is not connected")
        return self.client[self._db_name][self._events_name]

    async def ping(self) -> bool:
        try:
            if self.client is None:
                return False
            await self.client.admin.command("ping")
            return True
        except PyMongoError:
            return False

    async def _ensure_indexes(self) -> None:
        await self.messages.create_index(
            [("sessionName", ASCENDING), ("messageId", ASCENDING)],
            name="uniq_session_message",
            unique=True,
            partialFilterExpression={"messageId": {"$type": "string"}},
        )
        await self.messages.create_index([("timestamp", DESCENDING)], name="by_timestamp")
        await self.messages.create_index(
            [("chatId", ASCENDING), ("timestamp", DESCENDING)], name="by_chat"
        )
        await self.messages.create_index(
            [("direction", ASCENDING), ("timestamp", DESCENDING)], name="by_direction"
        )
        # Look a person up by number, or find everything that has a file.
        await self.messages.create_index(
            [("contactNumber", ASCENDING), ("timestamp", DESCENDING)], name="by_contact"
        )
        await self.messages.create_index(
            [("media.path", ASCENDING)], name="by_media", sparse=True
        )
        await self.events.create_index([("receivedAt", DESCENDING)], name="by_received")
        await self.events.create_index(
            [("idempotencyKey", ASCENDING)], name="uniq_idempotency", unique=True, sparse=True
        )

    # -- writes -------------------------------------------------------------

    async def upsert_message(
        self,
        *,
        session_name: str,
        message_id: str | None,
        set_fields: dict[str, Any],
        on_insert: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or merge a message row.

        `set_fields` should contain only values that are actually known -
        callers strip Nones so a later, thinner event never blanks out a field
        an earlier, richer one already stored.
        """
        now = utcnow()
        set_fields = {k: v for k, v in set_fields.items() if v is not None}
        set_fields["updatedAt"] = now

        insert_fields = {"createdAt": now, "sessionName": session_name}
        if on_insert:
            insert_fields.update({k: v for k, v in on_insert.items() if v is not None})
        # A field can't be in both $set and $setOnInsert.
        insert_fields = {k: v for k, v in insert_fields.items() if k not in set_fields}

        if not message_id:
            # No id to reconcile on (rare: engine returned nothing) - plain insert.
            doc = {**insert_fields, **set_fields, "sessionName": session_name}
            result = await self.messages.insert_one(doc)
            doc["_id"] = result.inserted_id
            return doc

        return await self.messages.find_one_and_update(
            {"sessionName": session_name, "messageId": message_id},
            {"$set": set_fields, "$setOnInsert": insert_fields},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

    async def update_status(
        self, *, session_name: str, message_id: str, status: str, extra: dict[str, Any] | None = None
    ) -> bool:
        """Apply a delivery receipt. Does not create a row for unknown ids."""
        fields = {"status": status, "updatedAt": utcnow()}
        if extra:
            fields.update({k: v for k, v in extra.items() if v is not None})
        result = await self.messages.update_one(
            {"sessionName": session_name, "messageId": message_id}, {"$set": fields}
        )
        return result.matched_count > 0

    async def get_message(self, *, session_name: str, message_id: str) -> dict[str, Any] | None:
        return await self.messages.find_one(
            {"sessionName": session_name, "messageId": message_id}
        )

    async def claim_media(self, *, session_name: str, message_id: str) -> bool:
        """Take ownership of downloading this message's media, exactly once.

        Same atomic-update guard as the poll claim: the gateway redelivers an
        event it thinks failed, and without this the same photo would be fetched
        and written two or three times.
        """
        result = await self.messages.update_one(
            {
                "sessionName": session_name,
                "messageId": message_id,
                "mediaClaimedAt": {"$exists": False},
            },
            {"$set": {"mediaClaimedAt": utcnow()}},
        )
        return result.modified_count == 1

    async def record_media(
        self, *, session_name: str, message_id: str, media: dict[str, Any]
    ) -> None:
        await self.messages.update_one(
            {"sessionName": session_name, "messageId": message_id},
            {"$set": {"media": media, "updatedAt": utcnow()}},
        )

    async def claim_polled(self, *, session_name: str, poll_message_id: str) -> bool:
        """Take ownership of one polled message id, exactly once, ever.

        The id is the document key, so the insert either succeeds or collides -
        no read-then-write window. Kept in MongoDB rather than memory so a
        restart does not resend whatever the endpoint still remembers.
        """
        try:
            await self.client[self._db_name]["poll_claims"].insert_one(
                {"_id": f"{session_name}:{poll_message_id}", "claimedAt": utcnow()}
            )
            return True
        except PyMongoError as exc:
            if "duplicate key" in str(exc).lower():
                return False
            raise

    async def record_event(self, payload: dict[str, Any], headers: dict[str, str]) -> None:
        """Keep the raw webhook envelope for auditing/debugging."""
        doc = {
            "receivedAt": utcnow(),
            "event": payload.get("event"),
            "sessionId": payload.get("sessionId"),
            "idempotencyKey": payload.get("idempotencyKey"),
            "deliveryId": payload.get("deliveryId"),
            "dispatchedAt": payload.get("timestamp"),
            "retryCount": headers.get("x-openwa-retry-count"),
            "data": payload.get("data"),
        }
        try:
            await self.events.insert_one(doc)
        except PyMongoError as exc:
            # A duplicate idempotency key just means OpenWA retried - not an error.
            if "duplicate key" not in str(exc).lower():
                log.warning("could not store raw event: %s", exc)

    # -- reads --------------------------------------------------------------

    async def recent(
        self, *, limit: int = 50, chat_id: str | None = None, direction: str | None = None
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if chat_id:
            query["chatId"] = chat_id
        if direction:
            query["direction"] = direction
        cursor = self.messages.find(query).sort("timestamp", DESCENDING).limit(limit)
        return [_jsonable(doc) async for doc in cursor]


def _jsonable(doc: dict[str, Any]) -> dict[str, Any]:
    doc = dict(doc)
    doc["_id"] = str(doc["_id"])
    for key, value in doc.items():
        if isinstance(value, datetime):
            doc[key] = value.isoformat()
    return doc
