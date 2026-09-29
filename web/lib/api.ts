// Everything the dashboard knows about the API, in one file.

// The business is named by the session cookie and by nothing else, so there
// is no slug here to configure, guess or pass. See `forwardSession` below.
export const SESSION_COOKIE = "aisales_session";

export type Role = "customer" | "ai" | "staff" | "system";

export type Message = {
  id: number;
  role: Role;
  body: string;
  created_at: string;
  meta?: Record<string, unknown>;
};

export type Conversation = {
  id: string;
  status: "ai" | "human" | "closed";
  needs_attention: boolean;
  attention_reason: string | null;
  last_message_at: string;
  phone_e164: string;
  last_body: string | null;
};

export type Lead = {
  id: string;
  tier: "hot" | "warm" | "cold";
  reason: string;
  intent: string | null;
  confidence: number | null;
  updated_at: string;
  phone_e164: string;
  name: string | null;
  status: string;
};

export type Order = {
  id: string;
  reference: string;
  status: string;
  total_kobo: number;
  paid_at: string | null;
  created_at: string;
  phone_e164: string;
  name: string | null;
  items: number;
};

export type Status = {
  business: string;
  slug: string;
  channel: string;
  models: string[];
  attention: number;
  payments: boolean;
  offline: boolean;
  owner: { name: string; business: string; initials: string; configured: boolean };
  suspended: boolean;
  suspended_reason: string | null;
  user: { email: string; name: string | null; role: "owner" | "staff"; is_operator: boolean };
};

/** Who is signed in, with no reference to a business. The one shape that is
 * meaningful for an operator, who may belong to none. */
export type Principal = {
  id: string;
  email: string;
  name: string | null;
  role: string;
  business: string | null;
  is_operator: boolean;
};

export type OperatorBusiness = {
  id: string;
  slug: string;
  name: string;
  created_at: string;
  suspended: boolean;
  suspended_at: string | null;
  suspended_reason: string | null;
  members: number;
  open_threads: number;
  messages_30d: number;
  leads: number;
  orders: number;
  integrations: Record<string, { account: string; status: string }>;
};

export type Product = {
  id: string;
  sku: string;
  name: string;
  description: string;
  price_kobo: number;
  stock_qty: number | null;
  variants: Record<string, string>[];
  image_url: string | null;
  active: boolean;
  sold: number;
};

export type Customer = {
  id: string;
  phone_e164: string;
  name: string | null;
  notes: Record<string, unknown>;
  conversations: number;
  orders: number;
  spent_kobo: number;
  tier: "hot" | "warm" | "cold" | null;
  first_seen_at: string;
  last_seen_at: string;
};

export type Payment = {
  id: string;
  reference: string;
  amount_kobo: number;
  status: "pending" | "success" | "failed" | "abandoned";
  channel: string | null;
  order_reference: string | null;
  phone_e164: string | null;
  name: string | null;
  created_at: string;
  verified_at: string | null;
};

export type SearchResults = {
  customers: { id: string; phone_e164: string; name: string | null }[];
  orders: { id: string; reference: string; status: string; total_kobo: number; phone_e164: string }[];
  products: { id: string; sku: string; name: string; price_kobo: number; stock_qty: number | null }[];
};

/** Initials for an avatar, from whatever name is available. */
export function initials(text: string): string {
  const parts = text.replace(/[^A-Za-z0-9 ]/g, " ").split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

export type SeriesPoint = {
  day: string;
  conversations: number;
  leads: number;
  orders: number;
};

export type Overview = {
  days: number;
  since: string;
  compared_with: string;
  now: {
    conversations: number; leads: number; orders: number;
    payments: number; pending_kobo: number; collected_kobo: number;
  };
  before: {
    conversations: number; leads: number; orders: number;
    payments: number; pending_kobo: number; collected_kobo: number;
  };
  series: SeriesPoint[];
  top_products: { name: string; sold: number; kobo: number }[];
  needing_attention: number;
  most_asked: { intent: string; count: number }[];
  why_they_did_not_buy: { reason: string; count: number }[];
  guard_blocked: number;
};

export type ActivityEntry = {
  id: number;
  action: string;
  actor: string;
  detail: Record<string, unknown>;
  model: string | null;
  created_at: string;
  conversation_id: string | null;
  phone_e164: string | null;
};

export type FollowUp = {
  id: string;
  reason: string;
  status: "scheduled" | "sent" | "cancelled" | "skipped";
  attempt: number;
  skipped_reason: string | null;
  due_at: string;
  created_at: string;
  phone_e164: string;
  name: string | null;
  conversation_id: string;
};

/** A percentage change, or null when there is no baseline to compare against.
 *  Returning 0 for "no previous data" would read as "no growth", which is a
 *  different and wrong statement. */
export function delta(now: number, before: number): number | null {
  if (before === 0) return null;
  return Math.round(((now - before) / before) * 100);
}

/** "12m ago", "in 2h", or a date.
 *
 * Handles the future deliberately: a due date is in the future, and the naive
 * `now - then` version reported every scheduled follow-up as "just now" --
 * which on a screen whose whole job is "when will this happen" is worse than
 * showing nothing.
 */
export function relativeTime(iso: string): string {
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  const ahead = seconds < 0;
  const size = Math.abs(seconds);

  if (size < 60) return ahead ? "in a moment" : "just now";
  if (size < 3600) {
    const n = Math.floor(size / 60);
    return ahead ? `in ${n}m` : `${n}m ago`;
  }
  if (size < 86400) {
    const n = Math.floor(size / 3600);
    return ahead ? `in ${n}h` : `${n}h ago`;
  }
  return shortDate(iso);
}

export type Daily = {
  day: string;
  collected_kobo: number;
  payments: number;
  collected_previous_kobo: number;
  compared_with: string;
  conversations: number;
  orders: number;
  new_leads: number;
  needing_attention: number;
  most_asked: { intent: string; count: number }[];
  why_they_did_not_buy: { reason: string; count: number }[];
  guard_blocked: number;
};

/** Kobo to naira, always with tabular digits. Money is an integer count of
 *  kobo everywhere; this is the only place it is divided. */
export function naira(kobo: number): string {
  return `₦${(kobo / 100).toLocaleString("en-NG", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  })}`;
}

export function shortTime(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-NG", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function shortDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-NG", {
    day: "numeric",
    month: "short",
  });
}

// Where to send a request depends on where it is made.
//
// In the browser, a relative `/api` path goes back through the Next rewrite,
// which keeps it same-origin and avoids a CORS preflight. On the server -- in
// a server component -- there is no page origin to be relative to, and Node's
// fetch rejects a relative URL outright, so the API has to be addressed
// directly. Getting this wrong fails only the server-rendered pages, which
// looks like "some screens are broken" rather than like a URL bug.
const SERVER_API = process.env.AISALES_API ?? "http://127.0.0.1:8100";

function url(path: string): string {
  return typeof window === "undefined" ? `${SERVER_API}${path}` : `/api${path}`;
}

// In the browser the cookie rides along by itself, because the rewrite keeps
// every call same-origin. A server component has no cookie jar, so the session
// has to be read off the incoming request and forwarded explicitly -- without
// this every server-rendered page looks signed out while the client ones work,
// which reads as "some screens are broken" rather than as a missing header.
async function forwardSession(): Promise<HeadersInit> {
  if (typeof window !== "undefined") return {};
  const { cookies } = await import("next/headers");
  const token = (await cookies()).get(SESSION_COOKIE)?.value;
  return token ? { cookie: `${SESSION_COOKIE}=${token}` } : {};
}

export class Unauthorized extends Error {}
export class ApiError extends Error {}

async function send<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(url(path), {
    cache: "no-store",
    ...init,
    headers: { ...init.headers, ...(await forwardSession()) },
  });
  if (response.status === 401) {
    // A page, not a thrown error: a signed-out session is an ordinary state
    // with somewhere to go, not a crash to be caught.
    const { redirect } = await import("next/navigation");
    redirect("/sign-in");
  }
  if (!response.ok) {
    throw new ApiError((await response.text()) || `${response.status}`);
  }
  return response.json() as Promise<T>;
}

/** Like `get`, but returns null instead of redirecting on 401.
 *
 * The root layout needs to know whether there is a session in order to decide
 * whether to draw the shell at all. Routing through `get` would work by
 * accident -- swallowing the NEXT_REDIRECT its `redirect()` throws -- and
 * would break the day Next changes how that signal is delivered.
 */
export async function peek<T>(path: string): Promise<T | null> {
  const response = await fetch(url(path), {
    cache: "no-store",
    headers: await forwardSession(),
  });
  return response.ok ? (response.json() as Promise<T>) : null;
}

export function get<T>(path: string): Promise<T> {
  return send<T>(path, {});
}

export function post<T>(path: string, body: unknown): Promise<T> {
  return send<T>(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function patch<T>(path: string, body: unknown): Promise<T> {
  return send<T>(path, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}
