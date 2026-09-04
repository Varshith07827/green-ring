# OpenWA Bridge

Send WhatsApp messages by POSTing `{"id": "<phone>", "msg": "<text>"}` — from this machine, or from
anywhere via a queue — and archive every message, both directions, in MongoDB.

```
                         Remote desktop
   ┌────────────────────────────────────────────────────┐
   │  bridge (Python, :8000)                            │
   │    POST /send  {id, msg}  ──────────┐              │
   │    GET  POLL_URL every 3s ──────────┤              │
   │                                     ▼              │
   │  OpenWA gateway (Node, :2785) ── headless Chrome ──┼──► WhatsApp
   │                    │                               │
   │       events       ▼                               │
   └──────────────────────┬─────────────────────────────┘
                          ▼  every message, both directions
                       MongoDB
```

| Path                                    | What it is                                                          |
| --------------------------------------- | ------------------------------------------------------------------- |
| `repo/`                                 | [OpenWA](https://github.com/rmyndharis/OpenWA), unmodified upstream |
| `bridge/`                               | The Python service — the part you talk to                           |
| `.env`                                  | The only file you configure — six settings                          |
| `start.sh` / `start.ps1`                | Starts everything — Linux / Windows                                 |
| `install-service.sh`                    | Registers both as systemd services (Linux)                          |
| `OpenWA-Bridge.postman_collection.json` | Import into Postman                                                 |

---

## Setting it up from scratch

Runs on **Linux** or **Windows**, natively — no Docker either way.

### Linux (Debian/Ubuntu)

```bash
git clone https://github.com/Varshith07827/green-ring.git
cd green-ring
./start.sh
```

Then, for a server you are not sitting in front of:

```bash
./install-service.sh
```

That registers both as systemd services, so they start on boot, restart on
failure, and survive you logging out. `./start.sh` alone runs them in your
terminal and stops when it closes — fine for a first look, wrong for a server.

Logs are `journalctl -u openwa-gateway -f` and `journalctl -u openwa-bridge -f`.
Remove with `./install-service.sh --remove`.

### Windows

```powershell
git clone https://github.com/Varshith07827/green-ring.git
cd green-ring
.\start.ps1
```

### Both do the same thing

Check for **Node 22+**, **Python 3.10+** and a browser, and install what is missing — apt and
NodeSource on Linux, winget on Windows. Then ask three questions — your MongoDB URI, the queue URL to poll (blank to skip), and its bearer token — writes
`.env`, installs dependencies, builds, and starts both processes. It prints your `BRIDGE_API_KEY`;
that's the token Postman sends.

If `MONGO_URI` points at this machine and nothing is listening there, Windows offers to install
MongoDB; Linux tells you where to get it. A remote or Atlas URI is left alone either way — that's
somebody else's server.

On Linux, Chrome comes from Puppeteer's own Chrome for Testing download on x64, and the distro's
`chromium` on arm64, which has no Chrome for Testing build. The shared libraries it needs are
installed alongside.

When everything is already present it says nothing about any of this and goes straight to work.

`.env` is deliberately **not** in the repo, since it holds secrets. The gateway's own `repo\.env` is
**generated from it** on every run, which is what keeps the key those two processes share in step —
setting it by hand is where a fresh install usually goes wrong.

**Then, once:** open <http://localhost:2785>, start the `default` session and scan the QR from
WhatsApp → Settings → Linked devices → Link a device. The session already exists — the bridge creates
it at startup — so you only press start and scan. The pairing survives restarts
(`AUTO_START_SESSIONS=true`, session data in `repo\data\sessions`), so this is a one-time step.

Check it took:

```powershell
curl.exe http://localhost:8000/health
```

`{"ok":true,...,"session":{"status":"ready","phone":"91..."}}` means you are live.

Every run after the first is just `.\start.ps1` — it skips setup, install and build when they are
already done.

### The queue is not part of this

The Cloudflare Worker that holds queued messages is deployed separately and keeps running on its
own — a fresh clone of this repo neither installs nor touches it. All this side needs is `POLL_URL`
and `POLL_TOKEN` pointing at it.

---

## Sending

```
POST http://localhost:8000/send
Authorization: Bearer <BRIDGE_API_KEY from .env>
Content-Type: application/json

{ "id": "919876543210", "msg": "Hello from Postman" }
```

**One endpoint for every chat.** The recipient is `id` in the body — there is no per-chat URL to
bind, register, or keep in step. Any chat reachable from the linked number can be messaged
immediately.

```json
{
  "ok": true,
  "messageId": "true_919876543210@c.us_3EB0ABCD",
  "chatId": "919876543210@c.us",
  "status": "sent",
  "storedId": "66d3f1..."
}
```

**`id`** is the phone number *with country code*. `+91 98765-43210`, `919876543210` and
`00919876543210` all work — spaces, `+`, dashes and brackets are stripped. A group id
(`120363...@g.us`) passes through untouched, so the same endpoint sends to groups.

A number with no country code is **rejected**, so a message cannot silently go to the wrong country.
Set `DEFAULT_COUNTRY_CODE=91` in `.env` to accept bare national numbers instead.

**`msg`** is text, 1–4096 characters. `number`/`to`/`phone` and `message`/`text`/`body` are accepted
as aliases.

| Method | Path               | Auth  | Purpose                                                     |
| ------ | ------------------ | ----- | ----------------------------------------------------------- |
| `POST` | `/send`            | token | Send a message to any chat, by `id`                         |
| `GET`  | `/health`          | —     | Mongo + gateway + session state                             |
| `GET`  | `/messages`        | token | Recent messages from Mongo (`limit`, `chatId`, `direction`) |
| `GET`  | `/session`         | token | Full session detail                                         |
| `GET`  | `/qr`              | token | Pairing QR as a PNG data URL                                |
| `POST` | `/events/register` | token | Recreate the session + event subscription                   |
| `GET`  | `/docs`            | —     | Swagger UI                                                  |

Auth is `Authorization: Bearer <BRIDGE_API_KEY>` (`X-API-Key: <key>` is still accepted). `401` is a
missing or wrong token, `400` a bad number or a session that is not ready, `422` a malformed body.

> The bridge listens on `0.0.0.0:8000`, so anything that can route to this machine can reach it —
> other PCs on the LAN, or the internet if a port is forwarded to it. From a machine that *cannot*
> route here, a direct `POST /send` will not arrive; that direction needs the bridge reachable
> (LAN, VPN, port forward, or a tunnel).

---

## Sending from anywhere: the pull model

`POST /send` needs this machine to be reachable. Polling does not — the bridge dials **out**, so a PC
anywhere can queue a message on your server and it goes out from here with no tunnel, no open port
and nothing routable about this desktop.

```
  any PC ──POST {id,msg}──► your server ◄──GET every 3s── bridge ──► WhatsApp
                              (queue)      returns and
                                           forgets the message
```

Set `POLL_URL` in `.env` and it starts. One URL, two verbs: `POST` queues a message, `GET`
asks "anything waiting to go out?".

What a poll response may return:

```json
{ "id": "919876543210", "msg": "Hello", "_id": "queue-42" }
```

- **`id` is the destination.** `msg` is the text (`message`/`text`/`content`/`body`/`reply` also work),
  and `to`/`number`/`phone`/`chatId` also name the destination.
- **`_id` is the dedup key** — also `message_id`/`messageId`/`external_id`/`uid`. Optional but worth
  sending.
- An **array** sends every message in it, not just the first.
- An **empty body** or `{}` means nothing is waiting.

### Nothing without a recipient is ever sent

One rule closes off a whole family of accidents: **a poll response can only produce a message if it
names who the message is for.** No destination, nothing sent — no exceptions, no default, no way to
configure one.

It matters because a status body looks exactly like a message. `{"success":true,"message":"No
messages"}` has a `message` field, and without this rule the bridge would send a stranger the words
*"No messages"*. The same goes for `{"success":true,"message":"Message sent"}`, an error body, a bare
JSON string, and an HTML login page served where an API was expected — each is refused, and each is
logged saying why.

Refusing an envelope never means losing what it wraps: a real message nested inside one
(`{"success":true,"message":"OK","data":{"id":"91…","msg":"the real one"}}`) is still found and sent.

There used to be a `POLL_DEFAULT_ID` setting that supplied a recipient when the payload named none —
a leftover from the per-chat model. It was the only route by which a status envelope could reach a
real person, so it is gone.

### `id` means destination here — a deliberate break from winspark

In the system this replaces, `id` in the poll response was the *dedup key*
(`external_id = obj.get("id")`), and the destination came from which per-chat URL was polled. Now
that one URL serves every chat, `id` has to carry the recipient instead — so the dedup key was moved
to `_id` and friends, and `id` is **never** read as an identity. Had it stayed, the first message to a
number would send and every later message to that same number would be silently dropped as a
duplicate.

### Deduplication

1. **An explicit message id is authoritative** — seen before, skipped, permanently. The record lives
   in MongoDB (`poll_claims`), so a restart does not resend a backlog.
2. **Without one, only a consecutive repeat is suppressed** — the same text to the same chat twice in
   a row.
3. **An endpoint that has ever answered empty is exempt from rule 2**, because answering empty proves
   it dequeues, and a dequeuing endpoint never offers the same message twice.

Rule 3 exists because rule 2 does real damage to a dequeuing endpoint: suppressing a message there
does not defer it, it **destroys** it — the endpoint already dropped it from its queue to hand it
over. For the same reason the bridge **does not poll at all while the session is not `ready`**, and a
send that fails after a message was handed over is written to MongoDB with `status: "failed"` rather
than lost.

### The queue — deployed and live

A Hono + TypeScript Cloudflare Worker, **deployed and live** as `whatsapp-webhook-api`, serving both
verbs on `/wam`: `POST` queues a message, `GET` hands one over and marks it delivered. Messages wait
in a D1 database called `wa-queue`. Auth is its `API_TOKEN` secret, which matches `POLL_TOKEN` in
`.env`.

> **Its source is not in this repo** — it was removed in `ca7b0d9`. The deployed Worker keeps
> running regardless, but there is currently nothing to rebuild it from if it needs changing.

```
https://whatsapp-webhook-api.alonewalker07827.workers.dev/wam
```

Two details in the `GET` half matter. It **dequeues** — a row goes out once, then is marked
delivered; an endpoint that re-served the same row would make the bridge send it forever. And when
nothing is waiting it returns an **empty body (204)**, never a status envelope, for the reason in
*Nothing without a recipient is ever sent* above.

It uses D1 rather than KV on purpose — KV is eventually consistent, so two polls seconds apart can
both read the same pending row and send the message twice. The claim is
`UPDATE … WHERE delivered_at IS NULL`; a losing racer hands over nothing.

Queue a message from any PC:

```bash
curl.exe --% -X POST https://whatsapp-webhook-api.alonewalker07827.workers.dev/wam -H "Authorization: Bearer <API_TOKEN>" -H "Content-Type: application/json" -d "{\"id\":\"919876543210\",\"msg\":\"Hello\"}"
```

Answers `202 {"success":true,"message":"Message queued","data":{"id":"…"}}`, and the message goes out
within a poll interval.

Your own server works just as well — the Worker is only one implementation of the two verbs. Point
`POLL_URL` anywhere that honours the contract above.

---

## What lands in MongoDB

Database `openwa`, collection `messages`. One document per WhatsApp message, both directions:

```json
{
  "sessionName": "default",
  "messageId": "true_919876543210@c.us_3EB0ABCD",
  "direction": "out",
  "chatId": "919876543210@c.us",
  "phone": "919876543210",
  "body": "Hello from Postman",
  "type": "text",
  "status": "read",
  "fromMe": true,
  "timestamp": "2026-09-02T20:31:04Z",
  "source": "api",
  "createdAt": "...",
  "updatedAt": "..."
}
```

- **`direction`** is `in` or `out`. **`status`** moves `sent → delivered → read` as receipts arrive,
  or becomes `failed` / `revoked` / `edited`.
- **Messages you type on the phone itself are captured too**, so the archive is the whole
  conversation, not just API traffic.
- `source` is `api` (from `/send`), `poll` (collected from your queue), or `webhook` (seen by the
  engine — including messages you typed on your own phone).
- **`contactName` and `contactNumber`** are the other party, pulled out as top-level fields so you
  can query and index them. The name prefers your saved contact name over `pushName`, which is
  whatever they call themselves this week. The number falls back through the contact record, the
  gateway's `senderPhone` (for privacy-id `@lid` senders), then the chat id — and is `null` for a
  group, which has no single number.
- **`media`** is present on any message that carried a file:

```json
"media": {
  "path": "2026/09/04/917981149423_3EB0ABCD_4a1ac396a5.jpg",
  "absolutePath": "C:\Users\nlabs\Desktop\openwa\data\media\2026\09\04\...",
  "filename": "photo.jpg",
  "mimetype": "image/jpeg",
  "sizeBytes": 184203,
  "savedAt": "2026-09-04T00:31:00Z"
}
```

  Photos, voice notes, video and documents are fetched from the gateway and written under
  `data/media/`, laid out by date so no directory grows unmanageable. `path` is relative, so the
  archive survives the project moving; `absolutePath` is for opening it now. If a download fails the
  field holds `{"error": …}` instead — a file that could not be fetched says so on the row rather
  than only in a log.
- `(sessionName, messageId)` is unique and every write is an upsert, so the `/send` row and the
  engine's own event for it merge into one document and retries never duplicate.
- Raw event envelopes go to the `events` collection. Turn that off with `STORE_RAW_EVENTS=false`.

---

## Configuration

One file, `.env` at the root, holding the six settings that have no usable default. Everything else
falls back to a working default and only belongs in the file if you are changing it — `.env.example`
lists the full set, commented.

| Setting          | Note                                                                                  |
| ---------------- | ------------------------------------------------------------------------------------- |
| `MONGO_URI`      | Currently the **local MongoDB service** on this machine, verified working. Change it to archive elsewhere. |
| `POLL_URL`       | Your queue endpoint. Set to the deployed Worker — polling is on.                       |
| `POLL_TOKEN`     | Bearer token sent with each poll. Matches the Worker's `API_TOKEN` secret.             |
| `BRIDGE_API_KEY` | The bearer token Postman sends as `Authorization: Bearer <key>`.                       |
| `OPENWA_API_KEY` | Shared with the gateway. Adopted from `repo\data\.api-key` when the gateway has already seeded one. |
| `EVENTS_SECRET`  | Signs the gateway's internal event deliveries.                                         |

Useful optional ones: `POLL_INTERVAL` (3s — raise it if your endpoint is on shared hosting),
`MEDIA_ENABLED`, `MEDIA_DIR`, `MEDIA_MAX_BYTES` (25 MiB), `MEDIA_OUTBOUND` (off — your own sent
media is a copy you already have), and `DEFAULT_COUNTRY_CODE`.

Both `.env` files hold secrets — keep them off GitHub (`bridge\.gitignore` covers its own).

---

## Notes on this install

**Dependencies were installed with `npm ci --ignore-scripts`, then `node scripts/postinstall.js`.**
Necessary, not a shortcut: `better-sqlite3` has no install script, so npm auto-runs `node-gyp
rebuild`, which demands Visual Studio C++ Build Tools (several GB). The package already ships a
prebuilt `prebuilds/win32-x64.node` that its loader prefers over anything node-gyp would produce, so
skipping the build costs nothing. `postinstall.js` then applies the ten upstream
`whatsapp-web.js`/`baileys` patches a plain `--ignore-scripts` install would have skipped. `start.ps1`
does both in the right order, and adds Git's `patch.exe` to PATH for the patch step.

**Puppeteer never downloaded its own Chromium** (same reason), so `repo\.env` points
`PUPPETEER_EXECUTABLE_PATH` at the installed Google Chrome. Update that line if Chrome moves.

**`SSRF_ALLOWED_HOSTS=localhost,127.0.0.1` in `repo\.env` is load-bearing.** The gateway refuses to
register an event subscription pointing at a loopback address unless it is allowlisted, and the
bridge listens on loopback. Remove it and events stop arriving.

**Harmless Windows warnings at startup:** `chmod 0o600 failed ... ENOENT` and `Docker not available`.
Neither affects anything here.

## Keeping it running unattended

Run both as services with [NSSM](https://nssm.cc/) so they survive logout and restart on failure:

```powershell
nssm install OpenWA "C:\Program Files\nodejs\node.exe" "dist\main"
nssm set OpenWA AppDirectory "C:\Users\nlabs\Desktop\openwa\repo"

nssm install OpenWABridge "C:\Users\nlabs\Desktop\openwa\bridge\.venv\Scripts\python.exe" "-m app.main"
nssm set OpenWABridge AppDirectory "C:\Users\nlabs\Desktop\openwa\bridge"
```

Order does not matter: the bridge retries via `POST /events/register`, and the gateway retries
deliveries.

## Before you connect a real number

This drives WhatsApp Web through a reverse-engineered client, not Meta's official API. WhatsApp can
restrict or ban a number for automated use. **Use a dedicated number you can afford to lose**, warm
it up for a few days before sending in volume, and keep the pace human. `repo\README.md` has
upstream's full guidance.

## Testing without touching WhatsApp

```powershell
cd bridge
.\.venv\Scripts\python.exe scripts\selftest.py
```

89 checks over auth (both header styles), number parsing, the send path, every event branch, and the
poll loop's parsing, dedup rules, refusals and failure handling, plus contact resolution and media
filenames — with MongoDB and the gateway stubbed out.
