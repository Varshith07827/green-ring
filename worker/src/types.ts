export type Bindings = {
  /** Shared secret. Both the POST (Postman) and the GET (the bridge) send it. */
  API_TOKEN: string;
  /** D1 database holding the queue. See schema.sql. */
  DB: D1Database;
};

/** One row of the queue, as stored. */
export type QueueRow = {
  id: number;
  message_id: string;
  dest: string;
  text: string;
};
