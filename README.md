# OpenWA Bridge

Send WhatsApp messages by POSTing JSON. Archive every message, both directions, in MongoDB, with
photos and voice notes written to disk.

Runs natively on Linux or Windows. **No Docker.**

```
   your app ──POST /send──► bridge (:8000) ──► OpenWA gateway (:2785) ──► WhatsApp
                                  │
                                  ▼
                          MongoDB + data/media/
```

| Path                                    | What it is                                                          |
| --------------------------------------- | ------------------------------------------------------------------- |
| `repo/`                                 | [OpenWA](https://github.com/rmyndharis/OpenWA), unmodified upstream |
| `bridge/`                               | The Python service — the part you talk to                           |
| `.env`                                  | The only file you configure                                         |
| `start.sh` / `start.ps1`                | Sets up and starts everything                                       |
| `install-service.sh`                    | Registers both as systemd services (Linux)                          |
| `data/media/`                           | Photos, voice notes and documents, by date. Not in git.             |
| `OpenWA-Bridge.postman_collection.json` | Import into Postman                                                 |

---

## Setup

```bash
git clone https://github.com/Varshith07827/green-ring.git
cd green-ring
./start.sh                 # Linux    (.\start.ps1 on Windows)
```

It installs whatever is missing (Node 22+, Python 3.10+, Chrome), asks for your **MongoDB URI**,
writes `.env`, builds, and starts both processes — printing your `BRIDGE_API_KEY`.

**Then once:** open <http://localhost:2785>, start the `default` session, scan the QR from
WhatsApp → Settings → Linked devices. The pairing survives restarts.

```bash
curl http://localhost:8000/health      # "status":"ready" means you are live
```

For a server, `./install-service.sh` — starts on boot, restarts on failure, survives logout.

---

## Calling it

Everything that sends is `POST`, takes JSON, and uses the same auth header.

**curl** — set these once:

```bash
BRIDGE=http://localhost:8000
KEY=$(grep BRIDGE_API_KEY .env | cut -d= -f2)

curl -X POST $BRIDGE/<path> -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" -d '<body>'
```

**Postman** — import `OpenWA-Bridge.postman_collection.json`, then set two collection variables:
`baseUrl` (`http://localhost:8000`) and `apiKey` (your `BRIDGE_API_KEY`). For a new request:

| | |
| --- | --- |
| Method / URL | `POST` `{{baseUrl}}/<path>` |
| Authorization | Type **Bearer Token**, value `{{apiKey}}` |
| Body | **raw** → **JSON**, paste `<body>` |

Only `<path>` and `<body>` change between calls, which is what the table below gives.

---

## Every command

`id` is always **the chat** — a phone number with country code, or a group jid like
`120363...@g.us`. `messageId` is the WhatsApp id returned by `/send`.

| Path        | What it does                       | Body |
| ----------- | ---------------------------------- | ---- |
| `/send`     | Send text                          | `{"id":"919876543210","msg":"Hello"}` |
| `/send`     | Send a file already hosted         | `{"id":"919876543210","msg":"caption","media":"https://host/photo.jpg"}` |
| `/reply`    | Reply, quoting a message           | `{"id":"919876543210","messageId":"<id>","msg":"a reply"}` |
| `/react`    | React — `""` removes it            | `{"id":"919876543210","messageId":"<id>","emoji":"👍"}` |
| `/forward`  | Forward from one chat to another   | `{"id":"919876543210","from":"919111111111","messageId":"<id>"}` |
| `/location` | Drop a pin on the map              | `{"id":"919876543210","latitude":17.385,"longitude":78.487,"description":"Hyderabad"}` |
| `/contact`  | Send a contact card                | `{"id":"919876543210","name":"Bob","number":"919111111111"}` |
| `/poll`     | Send a poll (2–12 options)         | `{"id":"919876543210","question":"Lunch?","options":["Park","Beach"]}` |
| `/edit`     | Change text you sent               | `{"id":"919876543210","messageId":"<id>","msg":"corrected"}` |
| `/delete`   | Delete it                          | `{"id":"919876543210","messageId":"<id>","forEveryone":true}` |
| `/star`     | Star, or un-star with `false`      | `{"id":"919876543210","messageId":"<id>","star":true}` |
| `/pin`      | Pin for 24h / 7d / 30d             | `{"id":"919876543210","messageId":"<id>","durationSeconds":86400}` |
| `/unpin`    | Unpin                              | `{"id":"919876543210","messageId":"<id>"}` |

Reads — all `GET`, same Bearer token:

| Path               | Auth  | What it does                                                |
| ------------------ | ----- | ----------------------------------------------------------- |
| `/health`          | none  | Mongo + gateway + session state                             |
| `/messages`        | token | Recent messages from Mongo (`limit`, `chatId`, `direction`) |
| `/session`         | token | Full session detail                                         |
| `/qr`              | token | Pairing QR as a PNG data URL                                |
| `/docs`            | none  | Swagger UI — try any of these in the browser                |
| `/events/register` | token | `POST`. Recreate the session + event subscription           |

A worked example, so the pattern is concrete:

```bash
curl -X POST $BRIDGE/reply -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"id":"919876543210","messageId":"true_919876543210@c.us_3EB0ABCD","msg":"a reply"}'
```

**Groups need nothing special** — put the group jid in `id`. List yours with:

```bash
GKEY=$(grep OPENWA_API_KEY .env | cut -d= -f2)
SID=$(curl -s -H "X-API-Key: $GKEY" http://localhost:2785/api/sessions | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["id"])')
curl -s -H "X-API-Key: $GKEY" "http://localhost:2785/api/sessions/$SID/groups?limit=20"
```

**Aliases**, if an existing client sends different names: `number` / `to` / `phone` / `chatId` for
`id`, and `message` / `text` / `body` for `msg`.

---

## Uploading a file

The one call that is not plain JSON.

**curl:**

```bash
curl -X POST $BRIDGE/send -H "Authorization: Bearer $KEY" \
  -F id=919876543210 -F msg="a caption" -F file=@holiday.png
```

**Postman:** `POST {{baseUrl}}/send`, Body → **form-data**, three rows:

| Key    | Type | Value             |
| ------ | ---- | ----------------- |
| `id`   | Text | `919876543210`    |
| `msg`  | Text | `a caption`       |
| `file` | File | *(pick the file)* |

Leave `Content-Type` alone — Postman sets it.

**You never pick a message type.** The mimetype decides: `image/*` → photo, `video/*` → video,
`audio/*` → audio, anything else → document. Override with `"type"`: `image`, `video`, `audio`,
`voice`, `document`, `sticker`. **`voice`** sends audio as a real voice note — mic bubble with a
waveform — rather than a file attachment; use `audio/ogg; codecs=opus`.

Three things worth knowing:

- **Uploads are archived, URL sends are not.** Bytes that pass through the bridge are written to
  `data/media/` and the path comes back as `mediaPath`. A URL is fetched by the gateway instead, so
  `mediaPath` is `null` and `mediaInfo.sourceUrl` records where it came from.
- **Uploads cap near 24 MiB**, measured after base64 encoding. Larger than that: host the file and
  send its URL.
- **The bytes outrank the filename.** A file that is really HTML, sent as an image, is refused with
  a 400 — that is the `curl -o photo.jpg` case, where the URL returned an error page and curl wrote
  it under the name you asked for. A merely wrong extension is corrected instead: a PNG called
  `mystery.txt` sends as an image.

`media` also accepts a `data:` URI or raw base64.

---

## What lands in MongoDB

Database `openwa`, collection `messages`. One document per message, both directions — including
messages you type on the phone itself.

```json
{
  "messageId": "true_919876543210@c.us_3EB0ABCD",
  "direction": "out",
  "chatId": "919876543210@c.us",
  "phone": "919876543210",
  "contactName": "Bob",
  "contactNumber": "919876543210",
  "body": "Hello",
  "type": "text",
  "status": "read",
  "source": "api",
  "timestamp": "2026-09-02T20:31:04Z",
  "media": {
    "path": "2026/09/04/919876543210_3EB0ABCD_4a1ac396a5.jpg",
    "mimetype": "image/jpeg",
    "sizeBytes": 184203
  }
}
```

- `status` moves `sent → delivered → read`, or becomes `failed` / `revoked` / `edited`.
- `source` is `api` (you called the bridge) or `webhook` (the engine saw it).
- `contactName` prefers your saved contact name over `pushName`. `contactNumber` is `null` for a
  group, which has no single number.
- `media.path` is relative, so the archive survives the project moving. A failed download leaves
  `{"error": …}` there rather than nothing.
- `(sessionName, messageId)` is unique and every write is an upsert — retries never duplicate.
- Raw event envelopes go to the `events` collection; `STORE_RAW_EVENTS=false` turns that off.

**Actions record two different ways.** Reply, forward, location, contact and poll create their own
document. React, edit, delete, star and pin are merged into the row of the message they acted on — a
separate document per reaction would claim two messages where the chat has one. Two consequences:

- **`/edit` keeps the old text**, appended to `editHistory` with a timestamp.
- **`/delete` marks, it does not remove** — the row gains `deleted`, `deletedForEveryone` and
  `deletedAt`, and keeps its body.

---

## Configuration

One file, `.env` at the root. Four settings; everything else has a working default.
`.env.example` lists the full set, commented.

| Setting          | What it is                                                                  |
| ---------------- | --------------------------------------------------------------------------- |
| `MONGO_URI`      | Where messages are archived                                                 |
| `BRIDGE_API_KEY` | The bearer token you send from Postman                                      |
| `OPENWA_API_KEY` | Shared with the gateway; adopted from `repo/data/.api-key` if already seeded |
| `EVENTS_SECRET`  | Signs the gateway's event deliveries to the bridge                          |

Useful optional ones: `BRIDGE_PORT`, `BRIDGE_HOST`, `MEDIA_DIR`, `MEDIA_MAX_BYTES`,
`DEFAULT_COUNTRY_CODE`.

`repo/.env` is **generated** from this file on every run — don't edit it.

Response codes: `401` bad token · `400` bad number, unusable `media`, or session not ready ·
`413` file too large · `422` malformed body · `502` the gateway refused it.

---

## Running unattended

The start scripts run in your terminal and die when it closes. For a server:

```bash
./install-service.sh                       # install
journalctl -u openwa-bridge -f             # logs
sudo systemctl restart openwa-bridge       # restart
./install-service.sh --remove              # uninstall
```

On Windows, [NSSM](https://nssm.cc/) pointing at `repo\dist\main` and
`bridge\.venv\Scripts\python.exe -m app.main`.

**After a `git pull` that changes `bridge/requirements.txt`**, systemd will not reinstall for you:

```bash
bridge/.venv/bin/python -m pip install -r bridge/requirements.txt
```

`./start.sh` does this automatically; `systemctl restart` does not.

---

## Notes

**Use a dedicated number.** This drives WhatsApp Web through a reverse-engineered client, not Meta's
official API. WhatsApp can restrict or ban a number for automated use. Warm it up before sending in
volume and keep the pace human.

**Secure the ports before going live.** The bridge listens on `0.0.0.0:8000` and the dashboard on
`:2785`, both plain HTTP — your token crosses the network in clear text, and the dashboard controls
the account. Put them behind nginx with TLS, or restrict them with `ufw` and reach the dashboard
over an SSH tunnel:

```bash
ssh -L 2785:localhost:2785 user@server      # then browse http://localhost:2785
```

**Install oddities, all deliberate.** `npm ci --ignore-scripts` then `node scripts/postinstall.js`
by hand: npm would otherwise run a `node-gyp` build of `better-sqlite3`, which needs a full C++
toolchain, while the package already ships prebuilt binaries its loader prefers — but the upstream
engine patches still have to be applied. `PUPPETEER_EXECUTABLE_PATH` is set explicitly since
Puppeteer never downloads its own browser. `SSRF_ALLOWED_HOSTS=localhost,127.0.0.1` is load-bearing:
the gateway refuses to deliver events to a loopback address unless it is allowlisted, and removing
it makes incoming messages silently stop being recorded.

**Harmless startup noise:** `Docker not available`, and `chmod 0o600 failed … ENOENT` on Windows.

---

## Testing without touching WhatsApp

```bash
bridge/.venv/bin/python bridge/scripts/selftest.py          # Linux
bridge\.venv\Scripts\python.exe bridge\scripts\selftest.py  # Windows
```

**161 checks** with MongoDB and the gateway stubbed out, so it sends nothing and needs neither
running: auth on both header styles, number parsing, every send path, every event branch, media
detection and filenames, contact resolution, byte sniffing, and every route in the send family.
