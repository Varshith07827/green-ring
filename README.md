<div align="center">

# OpenWA Bridge

**A WhatsApp messaging API with a durable archive.**

Send text, media, polls and locations over HTTP. Every message, in both directions, is recorded in
MongoDB with its media on disk — including the ones typed on the phone itself.

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![MongoDB](https://img.shields.io/badge/MongoDB-archive-47A248?logo=mongodb&logoColor=white)](https://www.mongodb.com/)
[![Tests](https://img.shields.io/badge/tests-161%20passing-success)](#testing)
[![No Docker](https://img.shields.io/badge/Docker-not%20required-lightgrey?logo=docker&logoColor=white)](#running-it)

</div>

---

## What it is

[OpenWA](https://github.com/rmyndharis/OpenWA) is a TypeScript gateway that drives WhatsApp Web
through headless Chrome. It is powerful and it is shaped like WhatsApp — session UUIDs in every
path, one endpoint per message type, `chatId` in the gateway's own `@c.us` form.

This is a Python service in front of it that turns all of that into one predictable HTTP API, and
adds the thing the gateway does not do: **a durable, queryable archive**.

```
                 ┌──────────────────────────────────────────────┐
   POST /send    │                                              │
  ────────────►  │   Bridge  ·  FastAPI + pymongo  ·  :8000      │
   {id, msg}     │                                              │
                 │   • one API surface, 19 endpoints            │
                 │   • phone numbers → WhatsApp jids            │
                 │   • archives every message, both directions  │
                 └───────┬───────────────────────────▲──────────┘
                         │  REST                     │  signed webhooks
                         ▼                           │  (HMAC-SHA256)
                 ┌──────────────────────────────────────────────┐
                 │   OpenWA gateway  ·  NestJS  ·  :2785        │
                 │   headless Chrome ──────────────────────────────► WhatsApp
                 └──────────────────────────────────────────────┘
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
        MongoDB                data/media/
      (messages)          (photos, voice notes)
```

**Why a second service rather than a fork?** The gateway stays unmodified upstream, so it can be
updated without carrying a patch set. Everything opinionated — the addressing scheme, the archive,
the failure semantics — lives on this side of the boundary.

---

## The API

`id` is always the chat: a phone number with country code, or a group jid (`120363...@g.us`).
`messageId` is what `/send` returns.

| Endpoint    | Does                             | Body |
| ----------- | -------------------------------- | ---- |
| `/send`     | Send text                        | `{"id":"919876543210","msg":"Hello"}` |
| `/send`     | Send a file by URL               | `{"id":"...","msg":"caption","media":"https://host/photo.jpg"}` |
| `/send`     | Send a file by upload            | `multipart` · `id`, `msg`, `file` |
| `/reply`    | Reply, quoting a message         | `{"id":"...","messageId":"...","msg":"a reply"}` |
| `/react`    | React — `""` removes it          | `{"id":"...","messageId":"...","emoji":"👍"}` |
| `/forward`  | Forward between chats            | `{"id":"...","from":"...","messageId":"..."}` |
| `/location` | Drop a pin                       | `{"id":"...","latitude":17.385,"longitude":78.487}` |
| `/contact`  | Send a contact card              | `{"id":"...","name":"Bob","number":"..."}` |
| `/poll`     | Send a poll (2–12 options)       | `{"id":"...","question":"Lunch?","options":["Park","Beach"]}` |
| `/edit`     | Change text you sent             | `{"id":"...","messageId":"...","msg":"corrected"}` |
| `/delete`   | Delete it                        | `{"id":"...","messageId":"...","forEveryone":true}` |
| `/star`     | Star or un-star                  | `{"id":"...","messageId":"...","star":true}` |
| `/pin`      | Pin for 24h / 7d / 30d           | `{"id":"...","messageId":"...","durationSeconds":86400}` |
| `/unpin`    | Unpin                            | `{"id":"...","messageId":"..."}` |

Reads: `GET /health` (public), `/messages`, `/session`, `/qr`, and Swagger at `/docs`.

```bash
curl -X POST http://localhost:8000/send \
  -H "Authorization: Bearer $BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"id":"919876543210","msg":"Hello"}'
```

Only the path and the body change between calls. In Postman: import the bundled collection, set
`baseUrl` and `apiKey`, and every request above is `POST {{baseUrl}}/<path>` with Bearer auth and a
raw JSON body.

**Groups need no special handling** — the group jid goes in `id`. **File type is never chosen by the
caller**: the mimetype decides, `image/*` → photo, `video/*` → video, `audio/*` → audio, anything
else → document, with `"type": "voice"` available for a real voice note.

---

## Engineering notes

The parts that were not obvious, and what was decided.

### Two writers, one document

A message has two sources of truth that arrive out of order. `POST /send` knows what was sent and
gets a `messageId` back; the gateway then delivers a `message.sent` webhook for the *same* message,
possibly before the first write has returned.

`(sessionName, messageId)` is unique and every write is an upsert, so the two converge on one
document. `status` and `timestamp` are written **insert-only** — the API path never overwrites what
the engine reported. Delivery receipts then move the row `sent → delivered → read` without racing
the write that created it.

### The filename that was always the same

Media files are named from the counterparty and the message id. The first implementation took the
last underscore-separated segment of the id as the unique part.

WhatsApp outbound ids look like `true_2590…@lid_3EB051DEB650C17ECF2511_out`. That final segment is
the literal string `out` — **for every outbound message ever sent**. Each new file silently
overwrote the last.

The unit tests passed, because they used invented message ids that looked reasonable. A live send
caught it. The fix hashes the whole id rather than trusting any slice of it:

```python
def safe_stem(phone: str | None, message_id: str) -> str:
    who = UNSAFE.sub("", phone or "") or "chat"
    readable = UNSAFE.sub("", message_id)[-18:] or "msg"
    digest = hashlib.sha1(message_id.encode("utf-8")).hexdigest()[:10]
    return f"{who}_{readable}_{digest}"
```

The lesson kept: a test written from an imagined input tests the imagination.

### Bytes outrank filenames

`curl -o photo.jpg <url>` against a URL that answers 400 writes the error page to `photo.jpg`. Every
step afterwards believes the extension: the bridge sends it as an image, the engine hands WhatsApp an
HTML document, and the reply is `500 Internal server error` mentioning none of it.

Uploads are now checked against their own magic bytes. HTML sent as an image is refused with a 400
that names the problem; a merely wrong extension is corrected instead of rejected, since a PNG called
`mystery.txt` is still a PNG. An explicit `mimetype` from the caller wins over both.

### An error message that distinguishes the cases

The gateway answers `500 Internal server error` whether the URL it was told to fetch returned 403,
the bytes were not really media, or something genuinely broke inside it. Three different problems,
three different fixes, one message describing none of them.

A 5xx on a media send now carries the likely cause: a URL send names the URL and the `curl` that
checks it, a byte send names the type it was refused as. 4xx is passed through untouched — the
gateway's own message is already the useful one there.

### What was deliberately not built

The obvious next step was to preflight caller-supplied URLs before sending them, which would give an
even better error. It was not built: the bridge would then make arbitrary outbound requests from
inside the network on request, which trades an SSRF vector for a nicer message. The gateway already
fetches those URLs behind its own guard, and it can keep doing so.

### Deleting the queue

The original design polled an external queue every three seconds. The bridge was assumed to sit
behind NAT and be unreachable, so it dialled **out** and asked whether anyone wanted a message sent.

That assumption stopped being true. Along with the polling loop went a surprising amount of
machinery that existed only to make polling safe — a destination-or-nothing rule, two layers of
deduplication plus the flag that retired one of them, an atomic claim collection, and a readiness
backoff that stopped an unpaired session exhausting the gateway's rate limit.

**849 lines deleted, 19 added.** None of it had a job in a push-only bridge, and it was correct code
solving a problem that had gone away.

### An archive that does not rewrite history

Actions record two different ways. Reply, forward, location, contact and poll create their own
document. React, edit, delete, star and pin are merged into the row of the message they acted on — a
separate document per reaction would claim two messages where the conversation has one.

Two consequences follow from treating the archive as a record rather than a mirror:

- **`/edit` keeps the old text**, appended to `editHistory` with a timestamp.
- **`/delete` marks rather than removes** — `deleted`, `deletedForEveryone`, `deletedAt`, body
  intact. That a message was sent and then withdrawn is exactly what an archive exists to remember.

### Identity, when WhatsApp hides it

WhatsApp increasingly identifies people by a privacy id (`@lid`) rather than a phone number, and an
outbound message carries no `senderPhone` at all. Numbers are resolved through the gateway's contact
lookup behind a bounded 5,000-entry cache that **also caches misses** — an unresolvable id is a
permanent fact, and re-asking on every message would be the busiest call the bridge makes.

`contactName` prefers your saved contact name over `pushName`, which is whatever the sender calls
themselves this week.

### Failures that surface where someone is watching

Three install-time guards, each added after the failure it prevents:

- **Ubuntu 24.04's `t64` transition** renamed five of the Chrome runtime packages. `apt-get` exits
  non-zero on the first name it cannot find, `set -e` took the rest of setup with it, and `-qq`
  buried the reason among mirror warnings. Package names are now probed rather than assumed, *after*
  the index refresh — the original code read the cache one line before updating it.
- **`install-service.sh` checks the service user can write** to the data directories before writing
  the unit files. A root-owned directory passes every check and then fails once systemd starts the
  unit unprivileged, and `Restart=always` turns that into a boot loop whose error reads like a
  config mistake.
- **`start.sh` refuses to print "Running"** over a process that has already exited. It used to sleep
  three seconds and print a dashboard URL and a send command regardless, burying the real error
  dozens of lines above the part people read.

---

## Running it

```bash
git clone <repo> && cd openwa
./start.sh                 # Linux    (.\start.ps1 on Windows)
```

Installs what is missing — Node 22+, Python 3.10+, Chrome and its shared libraries — asks for a
MongoDB URI, writes `.env`, builds, and starts both processes. Then open
<http://localhost:2785> once and scan the QR; the pairing survives restarts.

```bash
./install-service.sh       # systemd: starts on boot, restarts on failure, survives logout
```

Configuration is one file with four values: `MONGO_URI`, `BRIDGE_API_KEY`, `OPENWA_API_KEY`,
`EVENTS_SECRET`. The gateway's own `repo/.env` is **generated** from it on every run, which is what
keeps the shared key from drifting.

**Security note:** the bridge listens on `0.0.0.0:8000` and the dashboard on `:2785`, both plain
HTTP. Put them behind TLS or a firewall before exposing them — the token crosses the network in
clear text and the dashboard controls the linked account.

---

## Testing

```bash
bridge/.venv/bin/python bridge/scripts/selftest.py
```

**161 checks, no external services.** MongoDB and the gateway are both stubbed, so the suite sends
nothing and needs neither running — it covers auth on both header styles, number normalisation,
every send path, every webhook event branch, media detection, filename generation, contact
resolution, byte sniffing, and every route in the send family.

Roughly 2,700 lines of application Python across nine modules.

---

## Status

**Shelved.** The host it ran on was lost; the code is complete and was working end to end —
text, media by URL and by upload, voice notes, documents, groups, and the full action set, all
archiving to MongoDB.

Known gaps, in the order they would be picked up: a passthrough for the ~140 remaining gateway
routes (group management, labels, channels, profile), TLS termination, and rate limiting on the
bridge's own endpoints.

**A caveat worth stating plainly:** this drives WhatsApp Web through a reverse-engineered client,
not Meta's official API. WhatsApp can restrict or ban a number used this way. It is a study in
integration and durability, not something to point at an account that matters.
