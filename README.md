# OpenWA Bridge

Send a WhatsApp message by POSTing `{"id": "<phone>", "msg": "<text>"}` and archive every message,
both directions, in MongoDB, with photos and voice notes written to disk.

Runs natively on Linux or Windows. **No Docker.**

```
   ┌─────────────────── your server ────────────────────┐
   │                                                    │
   │   bridge (Python, :8000)                           │
   │     POST /send   {id, msg}                         │
   │                     │                              │
   │                     ▼                              │
   │   OpenWA gateway (Node, :2785) ─ headless Chrome ──┼──► WhatsApp
   │            │                                       │
   │            ▼  every message, in and out            │
   │     ┌──────────────┬──────────────┐                │
   │  MongoDB        data/media/    (text, who,         │
   │  (messages)     (the files)     when, status)      │
   └────────────────────────────────────────────────────┘
```

| Path                                    | What it is                                                          |
| --------------------------------------- | ------------------------------------------------------------------- |
| `repo/`                                 | [OpenWA](https://github.com/rmyndharis/OpenWA), unmodified upstream |
| `bridge/`                               | The Python service — the part you talk to                           |
| `.env`                                  | The only file you configure — four settings                          |
| `start.sh` / `start.ps1`                | Sets up and starts everything — Linux / Windows                     |
| `install-service.sh`                    | Registers both as systemd services (Linux)                          |
| `data/media/`                           | Photos, voice notes and documents, by date. Not in git.             |
| `OpenWA-Bridge.postman_collection.json` | Import into Postman                                                 |

---

## Setting it up from scratch

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

### Both scripts do the same thing

They check for **Node 22+**, **Python 3.10+** and a browser, and install whatever is missing — apt
and NodeSource on Linux, winget on Windows. Then they ask one question — your MongoDB URI — and from
that write `.env`, install dependencies, build, and start both processes, printing your
`BRIDGE_API_KEY`, the token you send from Postman.

If `MONGO_URI` points at this machine and nothing is listening there, Windows offers to install
MongoDB; Linux tells you where to get it. A remote or Atlas URI is left alone either way — that's
somebody else's server.

On Linux, Chrome comes from Puppeteer's own Chrome for Testing download on x64, and the distro's
`chromium` on arm64, which has no Chrome for Testing build. The shared libraries it needs are
installed alongside.

When everything is already present it says nothing about any of this and goes straight to work.

`.env` is deliberately **not** in the repo, since it holds secrets. The gateway's own `repo/.env` is
**generated from it** on every run, which is what keeps the key those two processes share in step —
setting it by hand is where a fresh install usually goes wrong.

**Then, once:** open <http://localhost:2785>, start the `default` session and scan the QR from
WhatsApp → Settings → Linked devices → Link a device. The session already exists — the bridge creates
it at startup — so you only press start and scan. The pairing survives restarts
(`AUTO_START_SESSIONS=true`, session data in `repo/data/sessions`), so this is a one-time step.

Check it took:

```bash
curl http://localhost:8000/health
```

`{"ok":true,...,"session":{"status":"ready","phone":"91..."}}` means you are live.

Every run after the first is just the same start script — it skips setup, install and build when
they are already done.

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
- `source` is `api` (from `/send`) or `webhook` (seen by the engine — including messages you typed
  on your own phone).
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

One file, `.env` at the root, holding the four settings that have no usable default. Everything else
falls back to a working default and only belongs in the file if you are changing it — `.env.example`
lists the full set, commented.

| Setting          | Note                                                                                  |
| ---------------- | ------------------------------------------------------------------------------------- |
| `MONGO_URI`      | Currently the **local MongoDB service** on this machine, verified working. Change it to archive elsewhere. |
| `BRIDGE_API_KEY` | The bearer token Postman sends as `Authorization: Bearer <key>`.                       |
| `OPENWA_API_KEY` | Shared with the gateway. Adopted from `repo/data/.api-key` when the gateway has already seeded one. |
| `EVENTS_SECRET`  | Signs the gateway's internal event deliveries.                                         |

Useful optional ones: `MEDIA_ENABLED`, `MEDIA_DIR`, `MEDIA_MAX_BYTES` (25 MiB), `MEDIA_OUTBOUND`
(off — your own sent media is a copy you already have), and `DEFAULT_COUNTRY_CODE`.

Both `.env` files hold secrets — keep them off GitHub (`bridge\.gitignore` covers its own).

---

## Why the install looks unusual

**Dependencies go in with `npm ci --ignore-scripts`, then `node scripts/postinstall.js` by hand.**
Necessary, not a shortcut. `better-sqlite3` has no install script, so npm falls back to running
`node-gyp rebuild` — which wants a full C++ toolchain (Visual Studio Build Tools on Windows,
several GB). The package already ships prebuilt binaries for every platform this runs on, and its
loader prefers them over anything node-gyp would produce, so skipping the build costs nothing.
Running `postinstall.js` afterwards restores the ten upstream `whatsapp-web.js`/`baileys` patches a
plain `--ignore-scripts` install would have skipped — those do matter. Both start scripts do this in
the right order.

**Puppeteer therefore never downloads its own browser**, so `PUPPETEER_EXECUTABLE_PATH` in
`repo/.env` is set explicitly — to the installed Chrome on Windows, and on Linux to Puppeteer's
Chrome for Testing (x64) or the distro chromium (arm64). Regenerated on every run, so a machine
change is picked up automatically.

**`SSRF_ALLOWED_HOSTS=localhost,127.0.0.1` is load-bearing.** The gateway refuses to register an
event subscription pointing at a loopback address unless it is allowlisted, and the bridge listens on
loopback. Remove it and incoming messages silently stop being recorded.

**Harmless startup noise:** `Docker not available` on both platforms (container orchestration you are
not using), and `chmod 0o600 failed … ENOENT` on Windows, where the gateway hardens a file the way
Linux would. Neither affects anything.

## Keeping it running unattended

Both start scripts run the processes in your terminal, and they **die when it closes**. That is fine
for a first look and wrong for a server.

**Linux** — systemd, which is the whole reason `install-service.sh` exists:

```bash
./install-service.sh
```

Starts on boot, restarts on failure, survives logout, and caps the gateway's memory so one runaway
Chrome cannot take the machine down with it. Logs go to the journal:

```bash
journalctl -u openwa-gateway -f
journalctl -u openwa-bridge -f
```

**Windows** — [NSSM](https://nssm.cc/), pointing at this checkout:

```powershell
nssm install OpenWA "C:\Program Files\nodejs\node.exe" "dist\main"
nssm set OpenWA AppDirectory "<path>\repo"

nssm install OpenWABridge "<path>\bridge\.venv\Scripts\python.exe" "-m app.main"
nssm set OpenWABridge AppDirectory "<path>\bridge"
```

Start order does not matter either way: the bridge retries registration via `POST /events/register`,
and the gateway retries deliveries.

## Before you connect a real number

This drives WhatsApp Web through a reverse-engineered client, not Meta's official API. WhatsApp can
restrict or ban a number for automated use. **Use a dedicated number you can afford to lose**, warm
it up for a few days before sending in volume, and keep the pace human. `repo/README.md` has
upstream's full guidance.

## Testing without touching WhatsApp

```bash
bridge/.venv/bin/python bridge/scripts/selftest.py        # Linux
```

```powershell
bridge\.venv\Scripts\python.exe bridge\scripts\selftest.py
```

**77 checks**, with MongoDB and the gateway stubbed out, so it sends nothing and needs neither
running: auth on both header styles, number parsing, the send path, every event branch, media
detection and filenames, and contact resolution.
