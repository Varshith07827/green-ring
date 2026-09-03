import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { logger } from 'hono/logger';
import { Bindings } from './types';
import wamRoute from './routes/wam';

const app = new Hono<{ Bindings: Bindings }>();

app.use('*', logger());
app.use('*', cors());

// Mounted so /media or /bulk can be added the same way later.
app.route('/wam', wamRoute);

app.notFound((c) => {
  return c.json({ success: false, error: 'Endpoint not found' }, 404);
});

app.onError((err, c) => {
  console.error(`[Global Error] ${err.message}`);
  return c.json({ success: false, error: 'Internal Server Error' }, 500);
});

export default app;
