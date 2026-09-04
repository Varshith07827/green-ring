"""In-process test of the bridge's request handling.

Runs the real FastAPI app with MongoDB and OpenWA replaced by stubs, so it can
verify routing, auth, number validation, the send path, every event branch and
the poll loop without touching either service.

    .venv\\Scripts\\python.exe scripts/selftest.py
"""

import asyncio
import hashlib
import hmac
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import main as bridge  # noqa: E402
from app.openwa import OpenWAError  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  FAIL  {name} {detail}")


class FakeStore:
    """Enough of Store to observe what the app would have written."""

    def __init__(self) -> None:
        self.docs: dict[tuple[str, str], dict[str, Any]] = {}
        self.inserts: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.poll_claims: set[str] = set()

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def ping(self) -> bool:
        return True

    async def upsert_message(self, *, session_name, message_id, set_fields, on_insert=None):
        set_fields = {k: v for k, v in set_fields.items() if v is not None}
        if not message_id:
            doc = {"_id": f"insert-{len(self.inserts)}", **(on_insert or {}), **set_fields}
            self.inserts.append(doc)
            return doc
        key = (session_name, message_id)
        existing = self.docs.get(key)
        if existing is None:
            doc = {
                "_id": f"doc-{len(self.docs)}",
                "sessionName": session_name,
                "messageId": message_id,
                **{k: v for k, v in (on_insert or {}).items() if v is not None},
                **set_fields,
            }
            self.docs[key] = doc
        else:
            existing.update(set_fields)
            doc = existing
        return doc

    async def get_message(self, *, session_name, message_id):
        return self.docs.get((session_name, message_id))

    async def set_fields(self, *, session_name, message_id, fields):
        doc = self.docs.get((session_name, message_id))
        if doc is None:
            return False
        doc.update({k: v for k, v in fields.items() if v is not None})
        return True

    async def claim_media(self, *, session_name, message_id):
        doc = self.docs.get((session_name, message_id))
        if doc is None or "mediaClaimedAt" in doc:
            return False
        doc["mediaClaimedAt"] = True
        return True

    async def record_media(self, *, session_name, message_id, media):
        doc = self.docs.get((session_name, message_id))
        if doc is not None:
            doc["media"] = media

    async def claim_polled(self, *, session_name, poll_message_id):
        key = f"{session_name}:{poll_message_id}"
        if key in self.poll_claims:
            return False
        self.poll_claims.add(key)
        return True

    async def update_status(self, *, session_name, message_id, status, extra=None):
        doc = self.docs.get((session_name, message_id))
        if doc is None:
            return False
        doc["status"] = status
        if extra:
            doc.update({k: v for k, v in extra.items() if v is not None})
        return True

    async def record_event(self, payload, headers):
        self.events.append(payload)

    async def recent(self, *, limit=50, chat_id=None, direction=None):
        rows = list(self.docs.values())
        if chat_id:
            rows = [r for r in rows if r.get("chatId") == chat_id]
        if direction:
            rows = [r for r in rows if r.get("direction") == direction]
        return rows[:limit]


class FakeOpenWA:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_next = False
        self.counter = 0

    async def close(self) -> None:
        pass

    async def session_id(self, refresh: bool = False) -> str:
        return "fake-session-uuid"

    async def session_info(self) -> dict[str, Any]:
        return {"name": "default", "status": "ready", "phone": "919999999999"}

    async def qr(self) -> dict[str, Any]:
        return {"qrCode": "data:image/png;base64,AAAA"}

    async def send_text(self, chat_id: str, text: str) -> dict[str, Any]:
        if self.fail_next:
            self.fail_next = False
            raise OpenWAError("session is not active", status=400)
        self.counter += 1
        self.sent.append((chat_id, text))
        return {"messageId": f"true_{chat_id}_MSG{self.counter}"}

    async def ensure_webhook(self, url, events, secret):
        return {"action": "created", "webhook": {"id": "wh-1", "url": url, "events": events}}


def signed(body: dict[str, Any], secret: str) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-OpenWA-Signature": signature, "Content-Type": "application/json"}


async def run() -> int:
    settings = bridge.settings
    store = FakeStore()
    openwa = FakeOpenWA()
    bridge.store = store
    bridge.openwa = openwa

    key_header = {"Authorization": f"Bearer {settings.bridge_api_key}"}
    hook_path = settings.events_path
    secret = settings.events_secret

    transport = httpx.ASGITransport(app=bridge.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        print("\n-- auth --")
        r = await client.post("/send", json={"id": "919876543210", "msg": "hi"})
        check("send without a key is rejected", r.status_code == 401, f"got {r.status_code}")

        r = await client.post(
            "/send", json={"id": "919876543210", "msg": "hi"}, headers={"X-API-Key": "wrong"}
        )
        check("send with a wrong key is rejected", r.status_code == 401, f"got {r.status_code}")

        r = await client.post(
            "/send",
            json={"id": "919876543210", "msg": "hi"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        check("wrong bearer token is rejected", r.status_code == 401, f"got {r.status_code}")

        r = await client.post(
            "/send",
            json={"id": "919876543210", "msg": "hi"},
            headers={"Authorization": settings.bridge_api_key},
        )
        check(
            "bare token without the Bearer scheme is rejected",
            r.status_code == 401,
            f"got {r.status_code}",
        )

        r = await client.post(
            "/send",
            json={"id": "919876543210", "msg": "bearer auth"},
            headers={"Authorization": f"Bearer {settings.bridge_api_key}"},
        )
        check("Authorization: Bearer is accepted", r.status_code == 200, f"got {r.status_code}")

        r = await client.post(
            "/send",
            json={"id": "919876543210", "msg": "legacy header"},
            headers={"X-API-Key": settings.bridge_api_key},
        )
        check("X-API-Key still works", r.status_code == 200, f"got {r.status_code}")

        print("\n-- send --")
        r = await client.post("/send", json={"id": "+91 98765-43210", "msg": "hello"}, headers=key_header)
        body = r.json()
        check("send returns 200", r.status_code == 200, f"got {r.status_code}: {body}")
        check("number normalised to a jid", body.get("chatId") == "919876543210@c.us", str(body))
        check("message id returned", bool(body.get("messageId")), str(body))
        check("reached the gateway", openwa.sent[-1] == ("919876543210@c.us", "hello"), str(openwa.sent))
        check("stored outbound", any(d.get("direction") == "out" for d in store.docs.values()))
        sent_message_id = body["messageId"]

        r = await client.post("/send", json={"number": "919876543210", "text": "via aliases"}, headers=key_header)
        check("accepts number/text aliases", r.status_code == 200, f"got {r.status_code}: {r.text}")

        r = await client.post("/send", json={"id": "12345", "msg": "too short"}, headers=key_header)
        check("rejects a short number", r.status_code == 400, f"got {r.status_code}")

        r = await client.post("/send", json={"id": "919876543210"}, headers=key_header)
        check("rejects a missing msg", r.status_code == 422, f"got {r.status_code}")

        r = await client.post("/send", json={"id": "919876543210", "msg": ""}, headers=key_header)
        check("rejects an empty msg", r.status_code == 422, f"got {r.status_code}")

        openwa.fail_next = True
        r = await client.post("/send", json={"id": "919876543210", "msg": "will fail"}, headers=key_header)
        check("gateway 400 surfaces as 400", r.status_code == 400, f"got {r.status_code}")
        check(
            "failed send is recorded",
            any(d.get("status") == "failed" for d in store.inserts),
            str(store.inserts),
        )

        print("\n-- webhook --")
        inbound = {
            "event": "message.received",
            "timestamp": "2026-09-02T10:00:00.000Z",
            "sessionId": "fake-session-uuid",
            "idempotencyKey": "msg_default_IN1",
            "deliveryId": "dlv_1",
            "data": {
                "id": "false_919876543210@c.us_IN1",
                "from": "919876543210@c.us",
                "to": "919999999999@c.us",
                "body": "a reply from the customer",
                "type": "text",
                "timestamp": int(datetime.now(timezone.utc).timestamp()),
                "isGroup": False,
                "hasMedia": False,
            },
        }
        raw, headers = signed(inbound, secret)
        r = await client.post(hook_path, content=raw, headers=headers)
        check("inbound accepted", r.status_code == 200, f"got {r.status_code}: {r.text}")
        stored_in = store.docs.get(("default", "false_919876543210@c.us_IN1"))
        check("inbound stored", stored_in is not None)
        check("inbound direction", (stored_in or {}).get("direction") == "in", str(stored_in))
        check("inbound chat id is the sender", (stored_in or {}).get("chatId") == "919876543210@c.us")
        check("inbound phone extracted", (stored_in or {}).get("phone") == "919876543210")

        r = await client.post(hook_path, content=raw, headers={"X-OpenWA-Signature": "sha256=deadbeef"})
        check("bad signature rejected", r.status_code == 401, f"got {r.status_code}")

        r = await client.post(hook_path, content=raw, headers={"Content-Type": "application/json"})
        check("missing signature rejected", r.status_code == 401, f"got {r.status_code}")

        # A duplicate delivery (OpenWA retry) must not create a second row.
        before = len(store.docs)
        await client.post(hook_path, content=raw, headers=headers)
        check("retry does not duplicate", len(store.docs) == before, f"{before} -> {len(store.docs)}")

        ack = {
            "event": "message.ack",
            "sessionId": "fake-session-uuid",
            "data": {"messageId": sent_message_id, "status": "delivered", "ack": 2},
        }
        raw, headers = signed(ack, secret)
        r = await client.post(hook_path, content=raw, headers=headers)
        check("ack accepted", r.status_code == 200)
        check(
            "ack updated the sent message",
            store.docs[("default", sent_message_id)].get("status") == "delivered",
            str(store.docs[("default", sent_message_id)]),
        )

        revoked = {
            "event": "message.revoked",
            "sessionId": "fake-session-uuid",
            "data": {"id": "notification-id", "revokedId": sent_message_id, "chatId": "919876543210@c.us"},
        }
        raw, headers = signed(revoked, secret)
        await client.post(hook_path, content=raw, headers=headers)
        check(
            "revoke reconciles on revokedId",
            store.docs[("default", sent_message_id)].get("status") == "revoked",
        )

        outbound_from_phone = {
            "event": "message.sent",
            "sessionId": "fake-session-uuid",
            "data": {
                "id": "true_919876543210@c.us_PHONE1",
                "from": "919999999999@c.us",
                "to": "919876543210@c.us",
                "body": "typed on the phone itself",
                "type": "text",
                "timestamp": int(datetime.now(timezone.utc).timestamp()),
            },
        }
        raw, headers = signed(outbound_from_phone, secret)
        await client.post(hook_path, content=raw, headers=headers)
        phone_doc = store.docs.get(("default", "true_919876543210@c.us_PHONE1"))
        check("message sent from the phone is captured", phone_doc is not None)
        check("its direction is out", (phone_doc or {}).get("direction") == "out")
        check("its chat id is the recipient", (phone_doc or {}).get("chatId") == "919876543210@c.us")

        print("\n-- contact name and number --")
        from app import media as media_mod  # noqa: PLC0415
        from app import messages as msg_mod  # noqa: PLC0415

        rich = {
            "from": "917981149423@c.us",
            "to": "918985370703@c.us",
            "contact": {"name": "Alice Kumar", "pushName": "alice", "number": "+91 79811 49423"},
        }
        fields = msg_mod.message_fields("message.received", rich)
        check("saved contact name wins over pushName", fields["contactName"] == "Alice Kumar", str(fields["contactName"]))
        check(
            "contact number is normalised to digits",
            fields["contactNumber"] == "917981149423",
            str(fields["contactNumber"]),
        )

        push_only = {"from": "917981149423@c.us", "contact": {"pushName": "Bob"}}
        f2 = msg_mod.message_fields("message.received", push_only)
        check("falls back to pushName", f2["contactName"] == "Bob", str(f2["contactName"]))
        check(
            "falls back to the chat id for the number",
            f2["contactNumber"] == "917981149423",
            str(f2["contactNumber"]),
        )

        lid = {"from": "216298915164281@lid", "senderPhone": "917981149423", "contact": {}}
        f3 = msg_mod.message_fields("message.received", lid)
        check(
            "an @lid sender still yields a number via senderPhone",
            f3["contactNumber"] == "917981149423",
            str(f3["contactNumber"]),
        )

        group = {"from": "120363012345678901@g.us", "isGroup": True, "contact": {}}
        f4 = msg_mod.message_fields("message.received", group)
        check("a group has no contact number", f4["contactNumber"] is None, str(f4["contactNumber"]))
        check("no contact means no name, not a guess", f4["contactName"] is None, str(f4["contactName"]))

        print("\n-- media detection --")
        # The real payload shape: no hasMedia flag anywhere, a `media` object
        # with the file inlined as base64.
        real = {
            "id": "true_259094657142792@lid_AC044B45_out",
            "from": "918985370703@c.us",
            "chatId": "259094657142792@lid",
            "type": "image",
            "media": {"mimetype": "image/jpeg", "data": "aGVsbG8=", "filename": "photo.jpg"},
        }
        mf = msg_mod.message_fields("message.sent", real)
        check("a media message is detected without a hasMedia flag", mf["hasMedia"] is True, str(mf["hasMedia"]))
        check("mimetype captured", mf["mediaInfo"]["mimetype"] == "image/jpeg", str(mf["mediaInfo"]))
        check("inline flag set", mf["mediaInfo"]["inline"] is True, str(mf["mediaInfo"]))
        check("base64 extracted", msg_mod.inline_media(real) == "aGVsbG8=", str(msg_mod.inline_media(real)))

        # The bytes must never reach MongoDB - seven small images were 90% of
        # the collection when they did, and a real photo approaches the 16 MB
        # document ceiling on its own.
        check("stored payload drops the base64", "data" not in mf["raw"]["media"], str(mf["raw"]["media"]))
        check("but keeps the metadata", mf["raw"]["media"]["mimetype"] == "image/jpeg")
        check("the original dict is not mutated", real["media"]["data"] == "aGVsbG8=")

        plain = msg_mod.message_fields("message.received", {"from": "91@c.us", "type": "text", "body": "hi"})
        check("a text message is not media", plain["hasMedia"] is False, str(plain["hasMedia"]))
        check("and has no mediaInfo", plain["mediaInfo"] is None)

        omitted = msg_mod.message_fields(
            "message.received",
            {"from": "91@c.us", "type": "video", "media": {"mimetype": "video/mp4", "omitted": True}},
        )
        check("withheld media is still flagged", omitted["hasMedia"] is True)
        check("marked as omitted, so it falls back to download", omitted["mediaInfo"]["omitted"] is True)
        check("with no inline data", omitted["mediaInfo"]["inline"] is False)

        print("\n-- @lid resolution --")
        lookups: list[str] = []

        async def fake_phone(contact_id):
            lookups.append(contact_id)
            return "917981149423" if contact_id.endswith("@lid") else None

        openwa.contact_phone = fake_phone
        bridge.settings.media_enabled = False  # isolate resolution from the media path

        lid_event = {
            "event": "message.sent",
            "sessionId": "fake-session-uuid",
            "data": {
                "id": "true_259094657142792@lid_LIDTEST1_out",
                "from": "918985370703@c.us",
                "to": "259094657142792@lid",
                "chatId": "259094657142792@lid",
                "body": "sent to a privacy id",
                "type": "text",
                "timestamp": int(datetime.now(timezone.utc).timestamp()),
                "contact": {"pushName": "Data"},
            },
        }
        raw_lid, hdr_lid = signed(lid_event, secret)
        await client.post(hook_path, content=raw_lid, headers=hdr_lid)
        for _ in range(40):
            await asyncio.sleep(0.01)
            if lookups:
                break
        await asyncio.sleep(0.05)

        doc = store.docs.get(("default", "true_259094657142792@lid_LIDTEST1_out")) or {}
        check("an @lid chat triggers a lookup", lookups == ["259094657142792@lid"], str(lookups))
        check("the number lands on the message", doc.get("contactNumber") == "917981149423", str(doc.get("contactNumber")))
        check("and is marked as resolved", doc.get("contactIdResolved") is True)
        check("the real status is not clobbered", doc.get("status") == "sent", str(doc.get("status")))

        # A plain number needs no lookup at all.
        before = len(lookups)
        plain_event = json.loads(json.dumps(lid_event))
        plain_event["data"]["id"] = "true_917981149423@c.us_PLAIN1_out"
        plain_event["data"]["to"] = "917981149423@c.us"
        plain_event["data"]["chatId"] = "917981149423@c.us"
        raw_p, hdr_p = signed(plain_event, secret)
        await client.post(hook_path, content=raw_p, headers=hdr_p)
        for _ in range(20):
            await asyncio.sleep(0.01)
        check("a normal number is not looked up", len(lookups) == before, str(lookups[before:]))

        bridge.settings.media_enabled = True

        print("\n-- media filenames --")
        check("jpeg gets .jpg, not .jpe", media_mod.extension_for("image/jpeg", None) == ".jpg")
        check("ogg voice note gets .ogg", media_mod.extension_for("audio/ogg; codecs=opus", None) == ".ogg")
        check("pdf gets .pdf", media_mod.extension_for("application/pdf", None) == ".pdf")
        check("unknown type falls back to .bin", media_mod.extension_for("application/x-weird", None) == ".bin")
        check(
            "the sender's own filename wins",
            media_mod.extension_for("application/octet-stream", "quarterly report.XLSX") == ".xlsx",
        )

        # A raw WhatsApp id contains @ : and . - all illegal or awkward in a
        # Windows filename.
        stem = media_mod.safe_stem("917981149423", "true_917981149423@c.us_3EB0FCB1149F877297175B")
        check("the stem is filename-safe", media_mod.UNSAFE.search(stem) is None, stem)
        check("it keeps the number and the unique tail", stem.startswith("917981149423_"), stem)
        check(
            "two messages in one chat get different names",
            media_mod.safe_stem("91", "true_x_AAAA") != media_mod.safe_stem("91", "true_x_BBBB"),
        )
        # Real outbound ids end in a literal "_out", so anything derived from
        # the last underscore-separated segment collides for every one of them.
        out_a = media_mod.safe_stem("918985370703", "true_259094657142792@lid_3EB051DEB650C17ECF2511_out")
        out_b = media_mod.safe_stem("918985370703", "true_259094657142792@lid_3EB0AAAAAAAAAAAAAAAAAA_out")
        check("two outbound media files do not collide", out_a != out_b, f"{out_a} vs {out_b}")
        check("outbound stem is not just the _out suffix", not out_a.endswith("_out"), out_a)
        check(
            "a group with no number still gets a name",
            media_mod.safe_stem(None, "false_120363@g.us_ZZZ").startswith("chat_"),
            media_mod.safe_stem(None, "false_120363@g.us_ZZZ"),
        )

        import tempfile  # noqa: PLC0415
        from datetime import datetime as _dt  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = media_mod.save(
                root,
                b"\xff\xd8\xff-not-really-a-jpeg",
                phone="917981149423",
                message_id="true_917981149423@c.us_3EB0ABCD",
                mimetype="image/jpeg",
                filename=None,
                when=_dt(2026, 9, 4, tzinfo=timezone.utc),
            )
            check("file is written to disk", (root / saved.path).is_file(), saved.path)
            check("laid out by date", saved.path.startswith("2026/09/04/"), saved.path)
            check("path uses forward slashes", "\\" not in saved.path, saved.path)
            check(
                "size recorded",
                saved.size_bytes == len(b"\xff\xd8\xff-not-really-a-jpeg"),
                str(saved.size_bytes),
            )
            doc = saved.as_document(root)
            check("document carries a relative and an absolute path", bool(doc["path"]) and bool(doc["absolutePath"]))
            check("mimetype has no codec suffix", doc["mimetype"] == "image/jpeg", doc["mimetype"])

        print("\n-- poll parsing --")
        from app import poller as poll_mod  # noqa: PLC0415

        one = poll_mod.parse('{"id":"919876543210","msg":"Hello"}')
        check("parses {id, msg}", len(one) == 1 and one[0].dest == "919876543210" and one[0].text == "Hello", str(one))
        check("id is NOT taken as the dedup id", one[0].message_id == "", repr(one[0].message_id))

        many = poll_mod.parse('[{"id":"91111111111","msg":"a"},{"id":"92222222222","message":"b"}]')
        check("parses an array of messages", len(many) == 2, str(many))
        check(
            "each keeps its own destination",
            many[0].dest == "91111111111" and many[1].dest == "92222222222",
            str(many),
        )

        env = poll_mod.parse('{"data":{"to":"919876543210","text":"nested"}}')
        check("parses an envelope and the `to` alias", len(env) == 1 and env[0].text == "nested", str(env))

        with_id = poll_mod.parse('{"id":"919876543210","msg":"x","_id":"queue-42"}')
        check("reads the dedup id from _id", with_id[0].message_id == "queue-42", str(with_id))

        check("empty body yields nothing", poll_mod.parse("") == [])
        check("{} yields nothing", poll_mod.parse("{}") == [])
        no_dest = poll_mod.parse('{"msg":"orphan"}')
        check("a message with no destination is skipped", no_dest == [], str(no_dest))

        # --- the status-envelope trap, closed off for good ------------------
        # Nothing without a recipient is ever sent, so an endpoint's own
        # chatter cannot be relayed to a stranger no matter how it is shaped.
        check(
            "a status envelope is not mistaken for a message",
            poll_mod.parse('{"success":true,"message":"Message sent","data":{}}') == [],
        )
        check(
            "...nor an idle one",
            poll_mod.parse('{"success":true,"message":"No messages"}') == [],
        )
        check(
            "...nor an error body",
            poll_mod.parse('{"success":false,"error":"Endpoint not found"}') == [],
        )
        check(
            "...nor a bare string",
            poll_mod.parse('"just some text"') == [],
        )
        check("plain text is never a message", poll_mod.parse("bare text") == [])
        check(
            "an HTML page is never a message",
            poll_mod.parse("<!DOCTYPE html><html><body>Please log in</body></html>") == [],
        )

        # A real message wrapped in a status envelope must still be found -
        # refusing the envelope must not mean losing what it carries.
        wrapped = poll_mod.parse(
            '{"success":true,"message":"OK","data":{"id":"919876543210","msg":"the real one"}}'
        )
        check(
            "a message wrapped in a status envelope is still delivered",
            len(wrapped) == 1
            and wrapped[0].dest == "919876543210"
            and wrapped[0].text == "the real one",
            str(wrapped),
        )

        # Exactly what the Worker hands back: a bare object, destination in
        # `id`, dedup key in `_id`, and no status envelope around it.
        worker_shape = poll_mod.parse('{"id":"919876543210","msg":"Hello","_id":"abc-123"}')
        check(
            "the Worker's GET payload parses as one message",
            len(worker_shape) == 1
            and worker_shape[0].dest == "919876543210"
            and worker_shape[0].text == "Hello"
            and worker_shape[0].message_id == "abc-123",
            str(worker_shape),
        )

        print("\n-- poll delivery --")
        bridge.settings.poll_url = "https://queue.example/wam"
        bridge.poll_state.last_text.clear()
        bridge.poll_state.endpoint_dequeues = False
        bridge.poll_state.session_ready = True

        queue: list[str] = []

        async def fake_poll(url):
            # The Worker answers 204 with an empty body when idle, 200 with a
            # message otherwise - both are modelled here.
            body = queue.pop(0) if queue else ""
            return poll_mod.PollResult(
                ok=True,
                status_code=200 if body else 204,
                messages=tuple(poll_mod.parse(body)),
                body=body,
            )

        bridge.poll_client.poll = fake_poll

        sent_before = len(openwa.sent)
        queue.append('{"id":"917981149423","msg":"queued one"}')
        await bridge._poll_once()
        check(
            "polled message is sent to the id in the body",
            openwa.sent[-1] == ("917981149423@c.us", "queued one"),
            str(openwa.sent[-1:]),
        )
        check("it is stored", any(d.get("source") == "poll" for d in store.docs.values()))

        # The trap: same destination, different text. The old code deduped on
        # `id`, which would suppress this second message entirely.
        queue.append('{"id":"917981149423","msg":"queued two"}')
        await bridge._poll_once()
        check(
            "a second message to the SAME id still sends",
            openwa.sent[-1] == ("917981149423@c.us", "queued two"),
            str(openwa.sent[-1:]),
        )
        check("both were sent", len(openwa.sent) - sent_before == 2, str(len(openwa.sent) - sent_before))

        # Rule 2: an unchanged answer means nothing new, until the endpoint
        # proves it dequeues.
        sent_before = len(openwa.sent)
        queue.append('{"id":"917981149423","msg":"queued two"}')
        await bridge._poll_once()
        check("an unchanged repeat is suppressed", len(openwa.sent) == sent_before, str(openwa.sent[-1:]))

        # Rule 3: an empty answer proves it dequeues, retiring rule 2.
        queue.append("")
        await bridge._poll_once()
        check("empty response marks the endpoint as dequeuing", bridge.poll_state.endpoint_dequeues is True)
        queue.append('{"id":"917981149423","msg":"queued two"}')
        await bridge._poll_once()
        check(
            "the same text now sends, because the endpoint dequeues",
            len(openwa.sent) == sent_before + 1,
            str(openwa.sent[-1:]),
        )

        # Rule 1: an explicit id is authoritative and permanent.
        sent_before = len(openwa.sent)
        queue.append('{"id":"917981149423","msg":"idempotent","_id":"queue-99"}')
        await bridge._poll_once()
        check("a message with an id sends once", len(openwa.sent) == sent_before + 1)
        queue.append('{"id":"917981149423","msg":"idempotent","_id":"queue-99"}')
        await bridge._poll_once()
        check("the same id is not sent twice", len(openwa.sent) == sent_before + 1, str(openwa.sent[-1:]))

        # Never dequeue what cannot be delivered.
        bridge.poll_state.session_ready = False
        polled = {"called": False}

        async def refuse_poll(url):
            polled["called"] = True
            return poll_mod.PollResult(ok=True, status_code=200)

        bridge.poll_client.poll = refuse_poll
        original_ready = bridge._session_is_ready

        async def not_ready():
            return False

        bridge._session_is_ready = not_ready
        await bridge._poll_once()
        check("no poll happens while the session is down", polled["called"] is False)
        bridge._session_is_ready = original_ready
        bridge.poll_client.poll = fake_poll
        bridge.poll_state.session_ready = True

        # A send that fails after the endpoint handed the message over must not
        # vanish silently.
        sent_before = len(openwa.sent)
        openwa.fail_next = True
        queue.append('{"id":"917981149423","msg":"will not send"}')
        await bridge._poll_once()
        check(
            "a failed polled send is recorded",
            any(
                d.get("source") == "poll" and d.get("status") == "failed"
                for d in store.inserts + list(store.docs.values())
            ),
            str(store.inserts[-1:]),
        )

        # An unusable destination is recorded rather than silently dropped.
        queue.append('{"id":"123","msg":"bad number"}')
        await bridge._poll_once()
        check(
            "an unusable destination is recorded",
            any("unusable destination" in str(d.get("error", "")) for d in store.inserts),
            str(store.inserts[-1:]),
        )

        bridge.settings.poll_url = ""

        print("\n-- reads --")
        r = await client.get("/messages", headers=key_header)
        check("messages listed", r.status_code == 200 and r.json()["count"] > 0, r.text[:120])
        r = await client.get("/messages")
        check("messages need a key", r.status_code == 401)
        r = await client.get("/health")
        check("health is public", r.status_code == 200, r.text[:120])
        check("health reports ready", r.json().get("ok") is True, r.text[:200])
        r = await client.get("/")
        check("root documents the send call", r.status_code == 200 and "send" in r.json())

    print("\n" + "=" * 60)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for failure in FAILED:
        print(f"  FAILED: {failure}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
