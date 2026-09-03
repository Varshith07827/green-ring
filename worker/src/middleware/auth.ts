import { Context, Next } from 'hono';
import { Bindings } from '../types';

/**
 * Compare in time independent of where the first difference falls.
 *
 * `token !== c.env.API_TOKEN` leaks, through timing, how much of a guess was
 * correct — enough to recover a secret one character at a time. This endpoint
 * can send WhatsApp messages as you, so it is worth the six extra lines.
 */
function safeEqual(a: string, b: string): boolean {
  const encoder = new TextEncoder();
  const left = encoder.encode(a);
  const right = encoder.encode(b);
  if (left.length !== right.length) return false;
  let diff = 0;
  for (let i = 0; i < left.length; i++) diff |= left[i] ^ right[i];
  return diff === 0;
}

export const authMiddleware = async (c: Context<{ Bindings: Bindings }>, next: Next) => {
  const authHeader = c.req.header('Authorization');

  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    return c.json({ success: false, error: 'Missing or invalid Authorization header' }, 401);
  }

  const token = authHeader.slice('Bearer '.length).trim();
  if (!c.env.API_TOKEN || !safeEqual(token, c.env.API_TOKEN)) {
    return c.json({ success: false, error: 'Unauthorized' }, 401);
  }

  await next();
};
