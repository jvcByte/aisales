import Link from "next/link";
import { GroupedBars } from "../../components/chart";
import { Icon } from "../../components/icons";
import { Card, Empty, Status, TableWrap } from "../../components/ui";
import {
  type ActivityEntry,
  type Conversation,
  type Customer,
  type Order,
  type Overview,
  type Product,
  type Status as SystemStatus,
  delta,
  get,
  initials,
  naira,
  relativeTime,
} from "../../lib/api";

export const dynamic = "force-dynamic";

const ACTION_LABEL: Record<string, string> = {
  order_created: "Order created",
  payment_link_sent: "Payment link sent",
  payment_confirmed: "Payment confirmed",
  lead_tagged: "Lead qualified",
  follow_up_sent: "Follow-up sent",
  escalated: "Escalated to a person",
  takeover: "Taken over by staff",
  handback: "Handed back to the AI",
  guard_blocked: "Reply blocked",
  payment_amount_mismatch: "Payment amount did not match",
  providers_exhausted: "No model available",
};

/** Machinery only. A feed of "looked something up" buries the four lines an
 *  owner would act on. */
const WORTH_SHOWING = new Set(Object.keys(ACTION_LABEL));

const ACTION_ICON: Record<string, string> = {
  order_created: "orders",
  payment_link_sent: "payments",
  payment_confirmed: "cash",
  lead_tagged: "leads",
  follow_up_sent: "followups",
  escalated: "leads",
  takeover: "customers",
  handback: "spark",
  guard_blocked: "shield",
  payment_amount_mismatch: "shield",
  providers_exhausted: "shield",
};

function tone(action: string): "good" | "warning" | "critical" | "neutral" {
  if (action === "payment_confirmed") return "good";
  if (["guard_blocked", "payment_amount_mismatch", "providers_exhausted"].includes(action)) {
    return "critical";
  }
  if (action === "escalated") return "warning";
  return "neutral";
}

function greeting(): string {
  // Read in Lagos: a shop in Kano and one in Lagos share a country but not a
  // clock, and "good morning" at 11pm reads as a machine.
  const hour = Number(new Date().toLocaleString("en-NG", {
    timeZone: "Africa/Lagos", hour: "2-digit", hour12: false,
  }));
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

/** A woven tile, for a product with no photograph.
 *
 * Drawn rather than faked: a stock photo of somebody else's fabric on Ada's
 * catalogue would be a lie about what she sells, and the pattern at least
 * reads as cloth. `products.image_url` is there for a real photo. */
function WeaveTile({ name }: { name: string }) {
  return (
    <span
      className="flex items-center justify-center text-[13px] font-semibold"
      style={{
        width: 52, height: 52, flex: "none",
        borderRadius: "var(--radius-md)",
        color: "color-mix(in oklab, var(--ink) 70%, transparent)",
        backgroundImage:
          "repeating-linear-gradient(45deg, rgba(255,255,255,0.05) 0 3px, transparent 3px 6px)," +
          "repeating-linear-gradient(-45deg, rgba(0,0,0,0.10) 0 3px, transparent 3px 6px)," +
          "linear-gradient(160deg, var(--series-2), var(--series-1))",
      }}
      aria-hidden
    >
      {initials(name)}
    </span>
  );
}

export default async function Dashboard({
  searchParams,
}: {
  searchParams: Promise<{ days?: string }>;
}) {
  const params = await searchParams;
  const days = Math.min(90, Math.max(1, Number(params.days) || 7));

  const [overview, conversations, activity, orders, products, customers, system] =
    await Promise.all([
      get<Overview>(`/insights/overview?days=${days}`),
      get<{ conversations: Conversation[] }>("/conversations").catch(() => ({ conversations: [] })),
      get<{ activity: ActivityEntry[] }>(`/activity?limit=40`).catch(() => ({ activity: [] })),
      get<{ orders: Order[] }>(`/orders`).catch(() => ({ orders: [] })),
      get<{ products: Product[] }>(`/catalogue`).catch(() => ({ products: [] })),
      get<{ customers: Customer[] }>(`/customers`).catch(() => ({ customers: [] })),
      get<SystemStatus>(`/status`).catch(() => null),
    ]);

  const n = overview.now;
  const since = `vs. last ${days} days`;
  const feed = activity.activity.filter((a) => WORTH_SHOWING.has(a.action)).slice(0, 6);
  const top = [...products.products].sort((a, b) => b.sold - a.sold).slice(0, 4);
  const owner = system?.owner.name ?? "there";

  const kpis = [
    { label: "Total Conversations", value: String(n.conversations),
      delta: delta(n.conversations, overview.before.conversations), icon: "conversations",
      tint: "var(--series-1)" },
    { label: "Qualified Leads", value: String(n.leads),
      delta: delta(n.leads, overview.before.leads), icon: "leads", tint: "var(--series-2)" },
    { label: "Orders Created", value: String(n.orders),
      delta: delta(n.orders, overview.before.orders), icon: "orders", tint: "var(--series-3)" },
    // Pending is money quoted and not yet settled, so the caption has to
    // describe *unpaid* orders. The earlier version read "from 7 completed
    // payments" under a pending figure, which is a sentence about two
    // different things.
    { label: "Revenue (Pending)", value: naira(n.pending_kobo), delta: null, icon: "cash",
      tint: "var(--good)", sub: n.pending_kobo > 0
        ? "quoted, not yet settled"
        : "nothing outstanding" },
  ];

  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_320px]">
      {/* ── main column ─────────────────────────────────────────────── */}
      <div className="min-w-0 space-y-4">
        {/* Hero. The mockup puts a blurred fabric photograph here. There is no
            photograph of Ada's shop, so this is a drawn gradient in the same
            register rather than a stock image of somebody else's. */}
        <section
          className="relative overflow-hidden p-5"
          style={{
            borderRadius: "var(--radius-lg)",
            border: "1px solid var(--line)",
            backgroundImage:
              "radial-gradient(120% 140% at 12% 10%, rgba(214,138,74,0.55) 0%, transparent 55%)," +
              "radial-gradient(120% 160% at 78% 90%, rgba(78,58,92,0.65) 0%, transparent 60%)," +
              "linear-gradient(105deg, #5c3a24 0%, #7a4c30 28%, #40364a 68%, #1d2029 100%)",
          }}
        >
          <div className="flex flex-wrap items-start gap-x-6 gap-y-4">
            <div className="min-w-[260px] flex-1 text-white">
              <h1 className="text-[22px] font-semibold tracking-tight">
                {greeting()}, {owner} <span aria-hidden>👋</span>
              </h1>
              <p className="mt-1.5 max-w-[420px] text-[13px] opacity-90">
                Your AI sales assistant is here, handling customer chats, qualifying
                leads, and turning conversations into sales.
              </p>
            </div>
            <div
              className="flex max-w-[300px] items-start gap-2.5 px-3.5 py-3 text-[12px] text-white"
              style={{
                borderRadius: "var(--radius-md)",
                background: "rgba(10,12,16,0.55)",
                border: "1px solid rgba(255,255,255,0.16)",
                backdropFilter: "blur(2px)",
              }}
            >
              <Icon name="shield" size={18} />
              <span>
                <span className="block font-medium">
                  “Never invent a price, stock figure, a policy, or a payment confirmation.”
                </span>
                <span className="mt-1 block opacity-75">Safety rule enforced in code</span>
              </span>
            </div>
          </div>
        </section>

        {/* KPI row */}
        <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {kpis.map((kpi) => (
            <div key={kpi.label} className="card p-4">
              <div className="flex items-start gap-3">
                <span
                  className="flex items-center justify-center"
                  style={{
                    width: 34, height: 34, flex: "none", borderRadius: "var(--radius-md)",
                    background: `color-mix(in oklab, ${kpi.tint} 20%, transparent)`,
                    color: kpi.tint,
                  }}
                  aria-hidden
                >
                  <Icon name={kpi.icon} size={17} />
                </span>
                <div className="min-w-0">
                  <div className="text-[12px]" style={{ color: "var(--muted)" }}>{kpi.label}</div>
                  <div className="mt-0.5 flex items-baseline gap-2">
                    <span className="num text-[24px] font-semibold leading-none tracking-tight">
                      {kpi.value}
                    </span>
                    {kpi.delta !== null && kpi.delta !== undefined && (
                      <span className="num text-[11px]"
                            style={{ color: kpi.delta > 0 ? "var(--good)" : kpi.delta < 0 ? "var(--critical)" : "var(--muted)" }}>
                        {kpi.delta > 0 ? "↑" : kpi.delta < 0 ? "↓" : "—"} {Math.abs(kpi.delta)}%
                      </span>
                    )}
                    {kpi.delta === null && (
                      <span className="text-[11px]" style={{ color: "var(--muted)" }}>—</span>
                    )}
                  </div>
                  <div className="mt-1 text-[11px]" style={{ color: "var(--faint)" }}>
                    {kpi.sub ?? (kpi.delta === null || kpi.delta === undefined
                      ? `no earlier data to compare`
                      : since)}
                  </div>
                </div>
              </div>
            </div>
          ))}
        </section>

        <div className="grid gap-4 lg:grid-cols-2">
          <Card
            title={<span className="flex items-center gap-2"><Icon name="conversations" size={15} /> Recent Conversations</span>}
            action={<Link href="/conversations" className="text-[12px]" style={{ color: "var(--muted)" }}>View all →</Link>}
            bodyClassName="p-0"
          >
            {conversations.conversations.length === 0 ? (
              <Empty>No conversations yet. Open <span className="num">/sim</span> and type as a customer.</Empty>
            ) : (
              <ul>
                {conversations.conversations.slice(0, 5).map((c) => (
                  <li key={c.id} className="flex items-start gap-3 px-4 py-3 hairline last:border-0">
                    <span
                      className="flex items-center justify-center text-[12px] font-semibold"
                      style={{
                        width: 34, height: 34, flex: "none", borderRadius: 999,
                        background: "color-mix(in oklab, var(--series-1) 24%, transparent)",
                      }}
                      aria-hidden
                    >
                      {initials(c.phone_e164.slice(-6))}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-2">
                        <span className="num text-[13px]">{c.phone_e164}</span>
                        <span className="num ml-auto text-[11px]" style={{ color: "var(--faint)" }}>
                          {relativeTime(c.last_message_at)}
                        </span>
                      </div>
                      <div className="mt-0.5 truncate text-[12px]" style={{ color: "var(--muted)" }}>
                        {c.last_body ?? "—"}
                      </div>
                    </div>
                    <Status tone={c.needs_attention ? "warning" : c.status === "human" ? "neutral" : "good"}>
                      {c.needs_attention ? "Follow Up" : c.status === "human" ? "Staff" : "Qualified"}
                    </Status>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card
            title={<span className="flex items-center gap-2"><Icon name="spark" size={15} /> Live Activity</span>}
            action={<Link href="/follow-ups" className="text-[12px]" style={{ color: "var(--muted)" }}>View all →</Link>}
            bodyClassName="p-0"
          >
            {feed.length === 0 ? (
              <Empty>Nothing has happened yet.</Empty>
            ) : (
              <ul>
                {feed.map((entry) => (
                  <li key={entry.id} className="flex items-start gap-3 px-4 py-3 hairline last:border-0">
                    <span
                      className="flex items-center justify-center"
                      style={{
                        width: 30, height: 30, flex: "none", borderRadius: 999,
                        background: `color-mix(in oklab, var(--${
                          tone(entry.action) === "good" ? "good"
                          : tone(entry.action) === "critical" ? "critical"
                          : tone(entry.action) === "warning" ? "warning" : "muted"
                        }) 20%, transparent)`,
                      }}
                      aria-hidden
                    >
                      <Icon name={ACTION_ICON[entry.action] ?? "spark"} size={15} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-2">
                        <span className="text-[12px] font-medium">{ACTION_LABEL[entry.action]}</span>
                        <span className="num ml-auto text-[11px]" style={{ color: "var(--faint)" }}>
                          {relativeTime(entry.created_at)}
                        </span>
                      </div>
                      <div className="mt-0.5 truncate text-[11px]" style={{ color: "var(--muted)" }}>
                        {entry.phone_e164 ?? "—"}
                        {typeof entry.detail?.tool === "string" ? ` · ${entry.detail.tool}` : ""}
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <Card
            title={<span className="flex items-center gap-2"><Icon name="catalogue" size={15} /> Top Products</span>}
            action={<Link href="/catalogue" className="text-[12px]" style={{ color: "var(--muted)" }}>View all →</Link>}
          >
            {top.length === 0 ? (
              <Empty>Nothing sold yet.</Empty>
            ) : (
              <ul className="flex gap-3 overflow-x-auto">
                {top.map((p) => (
                  <li key={p.id} className="w-[112px] flex-none">
                    <WeaveTile name={p.name} />
                    <div className="mt-2 text-[12px] leading-snug">{p.name}</div>
                    <div className="num mt-0.5 text-[12px] font-medium">{naira(p.price_kobo)}</div>
                    <div className="num text-[11px]" style={{ color: "var(--faint)" }}>{p.sold} sold</div>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title={<span className="flex items-center gap-2"><Icon name="spark" size={15} /> Quick Actions</span>}>
            <div className="grid gap-2.5 sm:grid-cols-2">
              {[
                { href: "/catalogue", label: "View Catalogue", icon: "catalogue", tint: "var(--series-1)" },
                { href: "/leads", label: "Manage Leads", icon: "leads", tint: "var(--series-2)" },
                { href: "/orders", label: "View Orders", icon: "orders", tint: "var(--series-3)" },
                { href: "/follow-ups", label: "Send Follow-up", icon: "followups", tint: "var(--warning)" },
              ].map((action) => (
                <Link
                  key={action.href}
                  href={action.href}
                  className="flex items-center gap-2.5 px-3 py-3 text-[13px]"
                  style={{
                    borderRadius: "var(--radius-md)",
                    border: "1px solid var(--line)",
                    background: `color-mix(in oklab, ${action.tint} 10%, transparent)`,
                  }}
                >
                  <span style={{ color: action.tint, display: "flex" }}>
                    <Icon name={action.icon} size={16} />
                  </span>
                  {action.label}
                </Link>
              ))}
            </div>
          </Card>
        </div>
      </div>

      {/* ── right rail ──────────────────────────────────────────────── */}
      <div className="min-w-0 space-y-4">
        <Card>
          <div className="flex items-center gap-3">
            <span
              className="flex items-center justify-center"
              style={{
                width: 34, height: 34, flex: "none", borderRadius: 999,
                border: "2px solid var(--good)", color: "var(--good)",
              }}
              aria-hidden
            >
              <Icon name="shield" size={16} />
            </span>
            <div>
              <div className="text-[13px] font-medium">System Online</div>
              <div className="text-[11px]" style={{ color: "var(--muted)" }}>
                API · {system?.models.length ? "Worker" : "no model"} · Database
              </div>
            </div>
          </div>
        </Card>

        <Card
          title={<span className="flex items-center gap-2"><Icon name="reports" size={15} /> Business Snapshot</span>}
          action={<Period days={days} />}
          bodyClassName="p-0"
        >
          <ul>
            {[
              { label: "Total Conversations", value: n.conversations, d: delta(n.conversations, overview.before.conversations), icon: "conversations" },
              { label: "Orders Created", value: n.orders, d: delta(n.orders, overview.before.orders), icon: "orders" },
              { label: "Revenue (Pending)", value: naira(n.pending_kobo), d: null, icon: "cash" },
            ].map((row) => (
              <li key={row.label} className="flex items-center gap-3 px-4 py-3 hairline last:border-0">
                <Icon name={row.icon} size={15} />
                <span className="text-[12px]">{row.label}</span>
                <span className="num ml-auto text-[13px] font-medium">{row.value}</span>
                {row.d !== null && row.d !== undefined && (
                  <span className="num text-[11px]"
                        style={{ color: row.d > 0 ? "var(--good)" : row.d < 0 ? "var(--critical)" : "var(--muted)" }}>
                    {row.d > 0 ? "↑" : row.d < 0 ? "↓" : "—"} {Math.abs(row.d)}%
                  </span>
                )}
                {row.d === null && (
                  <span className="text-[11px]" style={{ color: "var(--faint)" }}>not settled</span>
                )}
              </li>
            ))}
          </ul>
        </Card>

        <Card
          title={<span className="flex items-center gap-2"><Icon name="reports" size={15} /> Sales Performance</span>}
          action={<Period days={days} />}
        >
          {overview.series.some((d) => d.conversations || d.leads || d.orders) ? (
            <GroupedBars data={overview.series} height={130} />
          ) : (
            <Empty>No activity in the last {days} days.</Empty>
          )}
        </Card>

        <Card
          title={<span className="flex items-center gap-2"><Icon name="orders" size={15} /> Recent Orders</span>}
          action={<Link href="/orders" className="text-[12px]" style={{ color: "var(--muted)" }}>View all →</Link>}
          bodyClassName="p-0"
        >
          {orders.orders.length === 0 ? (
            <Empty>No orders yet.</Empty>
          ) : (
            <TableWrap>
              <thead>
                <tr style={{ color: "var(--muted)" }}>
                  <th className="px-4 py-2 text-left font-normal">Order</th>
                  <th className="px-4 py-2 text-left font-normal">Customer</th>
                  <th className="px-4 py-2 text-right font-normal">Amount</th>
                  <th className="px-4 py-2 text-right font-normal">Status</th>
                </tr>
              </thead>
              <tbody>
                {orders.orders.slice(0, 5).map((o) => (
                  <tr key={o.id} style={{ borderTop: "1px solid var(--line-soft)" }}>
                    <td className="num px-4 py-2.5">{o.reference}</td>
                    <td className="num px-4 py-2.5">{o.name ?? o.phone_e164}</td>
                    <td className="num px-4 py-2.5 text-right">{naira(o.total_kobo)}</td>
                    <td className="px-4 py-2.5 text-right">
                      <Status tone={
                        o.status === "paid" || o.status === "fulfilled" ? "good"
                        : o.status === "cancelled" ? "critical" : "warning"
                      }>
                        {o.status.replace(/_/g, " ")}
                      </Status>
                    </td>
                  </tr>
                ))}
              </tbody>
            </TableWrap>
          )}
        </Card>

        <Card title={<span className="flex items-center gap-2"><Icon name="customers" size={15} /> Newest Customers</span>}
              action={<Link href="/customers" className="text-[12px]" style={{ color: "var(--muted)" }}>View all →</Link>}
              bodyClassName="p-0">
          {customers.customers.length === 0 ? (
            <Empty>No customers yet.</Empty>
          ) : (
            <ul>
              {customers.customers.slice(0, 4).map((c) => (
                <li key={c.id} className="flex items-center gap-3 px-4 py-2.5 hairline last:border-0">
                  <span className="num text-[12px]">{c.name ?? c.phone_e164}</span>
                  <span className="num ml-auto text-[12px]" style={{ color: "var(--muted)" }}>
                    {c.orders} order{c.orders === 1 ? "" : "s"}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}

/** The period control. A real link rather than a styled button: it changes the
 *  server-rendered window, so it works without JavaScript and the URL says
 *  what you are looking at. */
function Period({ days }: { days: number }) {
  const options = [7, 30, 90];
  return (
    <span className="flex items-center gap-1 text-[11px]" style={{ color: "var(--muted)" }}>
      {options.map((option) => (
        <Link
          key={option}
          href={`/?days=${option}`}
          className="px-1.5 py-0.5"
          style={{
            borderRadius: "var(--radius-sm)",
            background: option === days
              ? "color-mix(in oklab, var(--accent) 16%, transparent)" : "transparent",
            color: option === days ? "var(--ink)" : "var(--faint)",
          }}
        >
          {option}d
        </Link>
      ))}
    </span>
  );
}
