import { GroupedBars } from "../../../components/chart";
import { Card, Empty, Status, TableWrap } from "../../../components/ui";
import {
  type Daily,
  type Lead,
  type Overview,
  delta,
  get,
  naira,
} from "../../../lib/api";

export const dynamic = "force-dynamic";

export default async function Reports({
  searchParams,
}: {
  searchParams: Promise<{ days?: string }>;
}) {
  const params = await searchParams;
  const days = Math.min(90, Math.max(1, Number(params.days) || 30));

  const [overview, today, leads] = await Promise.all([
    get<Overview>(`/insights/overview?days=${days}`),
    get<Daily>(`/insights/daily`),
    get<{ leads: Lead[] }>(`/leads`),
  ]);

  const n = overview.now;
  const lost = leads.leads.filter((l) => l.status === "lost").length;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h1 className="text-[18px] font-semibold tracking-tight">Reports</h1>
        <span className="text-[12px]" style={{ color: "var(--muted)" }}>
          the last {days} days
        </span>
        <span className="ml-auto flex items-center gap-1 text-[11px]" style={{ color: "var(--muted)" }}>
          {[7, 30, 90].map((option) => (
            <a
              key={option}
              href={`/reports?days=${option}`}
              className="px-1.5 py-0.5"
              style={{
                borderRadius: "var(--radius-sm)",
                background: option === days ? "color-mix(in oklab, var(--accent) 16%, transparent)" : "transparent",
                color: option === days ? "var(--ink)" : "var(--faint)",
              }}
            >
              {option}d
            </a>
          ))}
        </span>
      </header>

      {/* The funnel the KPI row implies, made explicit. Each figure is a
          count of rows, so it cannot report something that did not happen. */}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {[
          { label: "Conversations", value: String(n.conversations), d: delta(n.conversations, overview.before.conversations) },
          { label: "Leads qualified", value: String(n.leads), d: delta(n.leads, overview.before.leads) },
          { label: "Orders created", value: String(n.orders), d: delta(n.orders, overview.before.orders) },
          { label: "Collected", value: naira(n.collected_kobo), d: delta(n.collected_kobo, overview.before.collected_kobo) },
          { label: "Awaiting payment", value: naira(n.pending_kobo), d: null },
        ].map((stat) => (
          <div key={stat.label} className="card p-4">
            <div className="text-[12px]" style={{ color: "var(--muted)" }}>{stat.label}</div>
            <div className="num mt-1 text-[20px] font-semibold leading-none">{stat.value}</div>
            <div className="mt-1.5 text-[11px]" style={{ color: "var(--faint)" }}>
              {stat.d !== null && stat.d !== undefined
                ? `${stat.d > 0 ? "↑" : stat.d < 0 ? "↓" : "—"} ${Math.abs(stat.d)}% vs previous ${days} days`
                : "not yet settled"}
            </div>
          </div>
        ))}
      </div>

      <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
        <Card title="Activity by day">
          {overview.series.some((d) => d.conversations || d.leads || d.orders) ? (
            <GroupedBars data={overview.series} height={190} />
          ) : (
            <Empty>No activity in the last {days} days.</Empty>
          )}
        </Card>

        <div className="min-w-0 space-y-4">
          <Card title="Most asked" bodyClassName="p-0">
            {overview.most_asked.length === 0 ? (
              <Empty>Nothing classified yet.</Empty>
            ) : (
              <ul>
                {overview.most_asked.map((row) => (
                  <li key={row.intent} className="flex items-center gap-3 px-4 py-2.5 hairline last:border-0">
                    <span className="text-[12px]">{row.intent.replace(/_/g, " ")}</span>
                    <span className="num ml-auto text-[12px]">{row.count}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="Why they did not buy" bodyClassName="p-0">
            {overview.why_they_did_not_buy.length === 0 ? (
              <Empty>{lost === 0 ? "No lost leads recorded." : "None in this window."}</Empty>
            ) : (
              <ul>
                {overview.why_they_did_not_buy.map((row) => (
                  <li key={row.reason} className="flex items-start gap-3 px-4 py-2.5 hairline last:border-0">
                    <span className="text-[12px]" style={{ color: "var(--muted)" }}>{row.reason}</span>
                    <span className="num ml-auto text-[12px]">{row.count}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card>
            <div className="text-[12px]" style={{ color: "var(--muted)" }}>Today</div>
            <div className="num mt-1 text-[20px] font-semibold">{naira(today.collected_kobo)}</div>
            <div className="mt-1 text-[11px]" style={{ color: "var(--faint)" }}>
              vs {today.compared_with} · {today.payments} payment{today.payments === 1 ? "" : "s"}
            </div>
            {/* The guard's own error rate. A guard that blocks often is a guard
                that is wrong often, and this is the only place it shows. */}
            <div className="mt-3 flex items-center gap-2">
              <Status tone={today.guard_blocked === 0 ? "good" : "warning"}>
                {today.guard_blocked} blocked
              </Status>
              <span className="text-[11px]" style={{ color: "var(--faint)" }}>
                safety check
              </span>
            </div>
          </Card>
        </div>
      </div>

      <Card title="Top products" bodyClassName="p-0">
        {overview.top_products.length === 0 ? (
          <Empty>Nothing sold yet.</Empty>
        ) : (
          <TableWrap>
            <thead>
              <tr style={{ color: "var(--muted)" }}>
                <th className="px-4 py-2.5 text-left font-normal">Product</th>
                <th className="px-4 py-2.5 text-right font-normal">Sold</th>
                <th className="px-4 py-2.5 text-right font-normal">Revenue</th>
              </tr>
            </thead>
            <tbody>
              {overview.top_products.map((p) => (
                <tr key={p.name} style={{ borderTop: "1px solid var(--line-soft)" }}>
                  <td className="px-4 py-2.5">{p.name}</td>
                  <td className="num px-4 py-2.5 text-right">{p.sold}</td>
                  <td className="num px-4 py-2.5 text-right font-medium">{naira(p.kobo)}</td>
                </tr>
              ))}
            </tbody>
          </TableWrap>
        )}
      </Card>
    </div>
  );
}
