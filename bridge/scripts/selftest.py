"""In-process test of the bridge's request handling.

Runs the real FastAPI app with MongoDB and OpenWA replaced by stubs, so it can
verify routing, auth, number validation, the send path and every event branch
without touching either service.

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
        self.media_sent: list[dict[str, Any]] = []
        self.fail_next = False
        self.fail_status = 400
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

    async def send_media(
        self,
        kind,
        chat_id,
        *,
        url=None,
        data_base64=None,
        mimetype=None,
        filename=None,
        caption=None,
        ptt=False,
    ):
        if self.fail_next:
            self.fail_next = False
            raise OpenWAError("Internal server error", status=self.fail_status)
        self.counter += 1
        self.media_sent.append(
            {
                "kind": kind,
                "chatId": chat_id,
                "url": url,
                "base64": data_base64,
                "mimetype": mimetype,
                "filename": filename,
                "caption": caption,
                "ptt": ptt,
            }
        )
        return {"messageId": f"true_{chat_id}_MED{self.counter}"}

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

        print("\n-- media sending --")

        import base64 as _b64  # noqa: PLC0415
        import tempfile as _tempfile  # noqa: PLC0415

        openwa.media_sent.clear()

        r = await client.post(
            "/send",
            json={"id": "919876543210", "msg": "on the roof", "media": "https://ex.com/a/roof.jpg"},
            headers=key_header,
        )
        check("url send accepted", r.status_code == 200, f"got {r.status_code} {r.text[:120]}")
        last = openwa.media_sent[-1]
        check("image endpoint chosen from the extension", last["kind"] == "image", last["kind"])
        check("the url is passed through, not the bytes", last["url"] and not last["base64"])
        check("msg becomes the caption", last["caption"] == "on the roof", str(last["caption"]))
        check("response names the type", r.json()["type"] == "image", r.text[:80])

        r = await client.post(
            "/send",
            json={"id": "919876543210", "media": "https://ex.com/clip.mp4"},
            headers=key_header,
        )
        check("media with no caption is allowed", r.status_code == 200, f"got {r.status_code}")
        check("video endpoint chosen", openwa.media_sent[-1]["kind"] == "video")

        r = await client.post(
            "/send",
            json={"id": "919876543210", "media": "https://ex.com/photo.jpg", "type": "document"},
            headers=key_header,
        )
        check("explicit type overrides the mimetype", openwa.media_sent[-1]["kind"] == "document")

        r = await client.post(
            "/send",
            json={"id": "919876543210", "media": "https://ex.com/note.ogg", "type": "voice"},
            headers=key_header,
        )
        check("voice sends as audio", openwa.media_sent[-1]["kind"] == "audio")
        check("voice sets ptt", openwa.media_sent[-1]["ptt"] is True)

        r = await client.post(
            "/send",
            json={"id": "919876543210", "media": "file:///etc/passwd"},
            headers=key_header,
        )
        check("a file:// url is refused", r.status_code == 400, f"got {r.status_code}")

        r = await client.post(
            "/send",
            json={"id": "919876543210", "media": "https://ex.com/a.jpg", "msg": "x" * 1100},
            headers=key_header,
        )
        check("an over-long caption is refused", r.status_code == 422, f"got {r.status_code}")

        r = await client.post(
            "/send",
            json={"id": "919876543210", "media": "not base64 and not a url!!"},
            headers=key_header,
        )
        check("junk in media is refused", r.status_code == 400, f"got {r.status_code}")

        # Anything the bridge holds the bytes for is archived on the way out.
        original_dir = settings.media_dir
        with _tempfile.TemporaryDirectory() as tmp_media:
            settings.media_dir = tmp_media

            payload = b"\xff\xd8\xffnot-really-a-jpeg-but-bytes"
            r = await client.post(
                "/send",
                json={
                    "id": "919876543210",
                    "msg": "inline",
                    "media": "data:image/jpeg;base64," + _b64.b64encode(payload).decode(),
                },
                headers=key_header,
            )
            check("data uri send accepted", r.status_code == 200, f"got {r.status_code} {r.text[:120]}")
            body = r.json()
            check("outbound media is archived", bool(body.get("mediaPath")), r.text[:120])
            check(
                "the archived file is on disk",
                (Path(tmp_media) / body["mediaPath"]).is_file(),
                str(body.get("mediaPath")),
            )
            check("mimetype came from the data uri", openwa.media_sent[-1]["mimetype"] == "image/jpeg")

            r = await client.post(
                "/send",
                data={"id": "919876543210", "msg": "from disk"},
                files={"file": ("holiday.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
                headers=key_header,
            )
            check("multipart upload accepted", r.status_code == 200, f"got {r.status_code} {r.text[:160]}")
            up = openwa.media_sent[-1]
            check("upload kind from its content type", up["kind"] == "image", up["kind"])
            check("upload filename preserved", up["filename"] == "holiday.png", str(up["filename"]))
            check("upload sent as base64", bool(up["base64"]) and not up["url"])
            check("upload is archived too", bool(r.json().get("mediaPath")), r.text[:120])

            r = await client.post(
                "/send",
                data={"id": "919876543210"},
                files={"file": ("notes.txt", b"plain text", "text/plain")},
                headers=key_header,
            )
            check("an unknown type becomes a document", openwa.media_sent[-1]["kind"] == "document")

            over = settings.send_max_encoded_bytes
            settings.send_max_encoded_bytes = 64
            r = await client.post(
                "/send",
                data={"id": "919876543210"},
                files={"file": ("big.bin", b"A" * 4096, "application/octet-stream")},
                headers=key_header,
            )
            check("an oversized upload is refused", r.status_code == 413, f"got {r.status_code}")
            check("the refusal suggests a url", "URL" in r.text or "url" in r.text, r.text[:120])
            settings.send_max_encoded_bytes = over

        settings.media_dir = original_dir

        r = await client.post("/send", data={"id": "919876543210"}, headers=key_header)
        check("a form with no file and no text is refused", r.status_code == 422, f"got {r.status_code}")

        openwa.fail_next = True
        r = await client.post(
            "/send",
            json={"id": "919876543210", "media": "https://ex.com/x.jpg"},
            headers=key_header,
        )
        check("a refused media send reports the failure", r.status_code >= 400, f"got {r.status_code}")
        check(
            "the failed media send is recorded",
            any(d.get("status") == "failed" and d.get("type") == "image" for d in store.inserts),
        )

        print("\n-- bytes over labels --")

        openwa.media_sent.clear()

        r = await client.post(
            "/send",
            data={"id": "919876543210"},
            files={"file": ("photo.jpg", b"<!DOCTYPE html><html>400 Bad Request</html>", "image/jpeg")},
            headers=key_header,
        )
        check("html named .jpg is refused", r.status_code == 400, f"got {r.status_code}")
        check("the refusal says it is HTML", "HTML" in r.text, r.text[:140])
        check("nothing was sent", not openwa.media_sent, str(openwa.media_sent))

        # An honest HTML file as a document is fine - only media types are refused.
        r = await client.post(
            "/send",
            data={"id": "919876543210", "type": "document"},
            files={"file": ("page.html", b"<html><body>a real page</body></html>", "text/html")},
            headers=key_header,
        )
        check("html as a document is allowed", r.status_code == 200, f"got {r.status_code}")

        # The bytes win over a wrong extension when the caller did not insist.
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        r = await client.post(
            "/send",
            data={"id": "919876543210"},
            files={"file": ("mystery.txt", png, "text/plain")},
            headers=key_header,
        )
        check("a mislabelled png is corrected", r.status_code == 200, f"got {r.status_code}")
        check("corrected to image/png", openwa.media_sent[-1]["mimetype"] == "image/png", str(openwa.media_sent[-1]["mimetype"]))
        check("and sent as an image", openwa.media_sent[-1]["kind"] == "image", openwa.media_sent[-1]["kind"])

        # An explicit mimetype is respected even when the bytes disagree.
        r = await client.post(
            "/send",
            data={"id": "919876543210", "mimetype": "application/octet-stream"},
            files={"file": ("thing.bin", png, "application/octet-stream")},
            headers=key_header,
        )
        check(
            "an explicit mimetype is not overridden",
            openwa.media_sent[-1]["mimetype"] == "application/octet-stream",
            str(openwa.media_sent[-1]["mimetype"]),
        )

        print("\n-- explained failures --")

        openwa.fail_next = True
        openwa.fail_status = 500
        r = await client.post(
            "/send",
            json={"id": "919876543210", "media": "https://blocked.example/photo.jpg"},
            headers=key_header,
        )
        check("a gateway 500 becomes a 502", r.status_code == 502, f"got {r.status_code}")
        check("the failing url is named", "blocked.example" in r.text, r.text[:200])
        check("and it suggests checking the url", "curl" in r.text, r.text[:200])

        openwa.fail_next = True
        openwa.fail_status = 500
        r = await client.post(
            "/send",
            data={"id": "919876543210"},
            files={"file": ("clip.mp4", b"\x00\x00\x00\x20ftypisom", "video/mp4")},
            headers=key_header,
        )
        check("a bytes send explains itself too", r.status_code == 502, f"got {r.status_code}")
        check("naming the type it refused", "video" in r.text, r.text[:200])
        check("without mentioning a url", "curl -sSI" not in r.text, r.text[:200])

        openwa.fail_next = True
        openwa.fail_status = 400
        r = await client.post(
            "/send",
            json={"id": "919876543210", "media": "https://ex.com/a.jpg"},
            headers=key_header,
        )
        check("a 4xx is passed through unchanged", r.status_code == 400, f"got {r.status_code}")
        openwa.fail_status = 400

        print("\n-- forms and numbers --")

        from app.numbers import to_chat_id as _to_chat_id  # noqa: PLC0415

        openwa.sent.clear()
        r = await client.post(
            "/send",
            data={"id": "919876543210", "msg": "sent as a form field"},
            headers=key_header,
        )
        check("urlencoded form send accepted", r.status_code == 200, f"got {r.status_code} {r.text[:120]}")
        check(
            "the form's text arrives intact",
            openwa.sent and openwa.sent[-1][1] == "sent as a form field",
            str(openwa.sent[-1:]),
        )

        check(
            "a 00 prefix is not given a second country code",
            _to_chat_id("00628123456789", "91") == "628123456789@c.us",
            _to_chat_id("00628123456789", "91"),
        )
        check(
            "a bare national number still gets the default code",
            _to_chat_id("9876543210", "91") == "919876543210@c.us",
            _to_chat_id("9876543210", "91"),
        )
        check(
            "a trunk zero is dropped before the country code",
            _to_chat_id("09876543210", "91") == "919876543210@c.us",
            _to_chat_id("09876543210", "91"),
        )
        check(
            "a group jid is passed through untouched",
            _to_chat_id("120363043211234567@g.us", "91") == "120363043211234567@g.us",
        )

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
