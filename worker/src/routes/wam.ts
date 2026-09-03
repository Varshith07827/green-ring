import { Hono } from 'hono';
import { Bindings, QueueRow } from '../types';
import { authMiddleware } from '../middleware/auth';

const wam = new Hono<{ Bindings: Bindings }>();

wam.use('*', authMiddleware);

// Field names accepted on the way in, so a caller that already says `message`
// or `to` does not have to be rewritten.
const TEXT_KEYS = ['msg', 'message', 'text', 'body'] as const;
const DEST_KEYS = ['id', 'to', 'number', 'phone', 'chatId'] as const;

function firstString(obj: Record<string, unknown>, keys: readonly string[]): string {
  for (const key of keys) {
    const value = obj[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  }
  return '';
}

/**
 * POST /wam — queue a message.
 *
 * This used to forward straight to WHATSAPP_API_URL and report whatever came
 * back as "Message sent". That could not work: the machine running WhatsApp
 * sits behind a home router with no inbound route, so there was nothing
 * reachable to forward to, and any 2xx from the URL — including an HTML login
 * page reached through a redirect — was reported as a successful send.
 *
 * The message is now held here until the bridge collects it (GET below).
 */
wam.post('/', async (c) => {
  const body = await c.req.json().catch(() => null);
  if (!body || typeof body !== 'object') {
    return c.json({ success: false, error: 'Body is not valid JSON' }, 400);
  }

  const dest = firstString(body as Record<string, unknown>, DEST_KEYS);
  const text = firstString(body as Record<string, unknown>, TEXT_KEYS);
  if (!dest || !text) {
    return c.json({ success: false, error: 'Missing required fields: id, msg' }, 400);
  }

  const messageId = crypto.randomUUID();
  await c.env.DB.prepare(
    'INSERT INTO queue (message_id, dest, text, created_at) VALUES (?, ?, ?, ?)'
  )
    .bind(messageId, dest, text, Date.now())
    .run();

  console.log(`[WAM] queued ${messageId} for ${dest}`);

  // "queued", not "sent" — it has not reached WhatsApp yet, and saying
  // otherwise misleads everything downstream that trusts this response.
  return c.json({ success: true, message: 'Message queued', data: { id: messageId } }, 202);
});

/**
 * GET /wam — the bridge collects one message.
 *
 * Two things this has to get right:
 *
 * 1. **It dequeues.** A row goes out once and is then marked delivered. The
 *    bridge treats an empty answer as proof this endpoint dequeues and stops
 *    suppressing repeated text, so an endpoint that re-served the same row
 *    would send it forever.
 *
 * 2. **Idle returns an empty body, never a status envelope.** A reply like
 *    `{"success":true,"message":"No messages"}` is indistinguishable from a
 *    queued message whose text happens to be "No messages" — the bridge reads
 *    `message` as the text to send, and those two words would go out to
 *    somebody. 204 with no body is unambiguous.
 */
wam.get('/', async (c) => {
  const row = await c.env.DB.prepare(
    'SELECT id, message_id, dest, text FROM queue WHERE delivered_at IS NULL ORDER BY id LIMIT 1'
  ).first<QueueRow>();

  if (!row) return c.body(null, 204);

  // The claim. If a concurrent poll took this row first, changes === 0 and we
  // hand over nothing rather than sending the same message twice.
  const claim = await c.env.DB.prepare(
    'UPDATE queue SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL'
  )
    .bind(Date.now(), row.id)
    .run();

  if (!claim.meta || claim.meta.changes === 0) return c.body(null, 204);

  console.log(`[WAM] handed ${row.message_id} to the bridge`);

  // `id` is the DESTINATION and `_id` the message's own identity. The bridge
  // reads them that way round and never treats `id` as a deduplication key —
  // doing so would send the first message to a number and silently drop every
  // later one.
  return c.json({ id: row.dest, msg: row.text, _id: row.message_id }, 200);
});

export default wam;
