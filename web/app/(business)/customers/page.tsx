import { Card, Empty, Status, TableWrap } from "../../../components/ui";
import { type Customer, get, initials, naira, relativeTime } from "../../../lib/api";

export const dynamic = "force-dynamic";

export default async function Customers() {
  const { customers } = await get<{ customers: Customer[] }>(`/customers`);
  const buyers = customers.filter((c) => c.orders > 0).length;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h1 className="text-[18px] font-semibold tracking-tight">Customers</h1>
        <span className="text-[12px]" style={{ color: "var(--muted)" }}>
          {customers.length === 0
            ? "nobody yet"
            : `${customers.length}, of whom ${buyers} have ordered`}
        </span>
      </header>

      <Card bodyClassName="p-0">
        {customers.length === 0 ? (
          <Empty>No customers yet.</Empty>
        ) : (
          <TableWrap>
            <thead>
              <tr style={{ color: "var(--muted)" }}>
                <th className="px-4 py-2.5 text-left font-normal">Customer</th>
                <th className="hidden px-4 py-2.5 text-left font-normal lg:table-cell">Notes the AI kept</th>
                <th className="hidden px-4 py-2.5 text-right font-normal md:table-cell">Chats</th>
                <th className="px-4 py-2.5 text-right font-normal">Orders</th>
                <th className="px-4 py-2.5 text-right font-normal">Spent</th>
                <th className="hidden px-4 py-2.5 text-right font-normal md:table-cell">Last seen</th>
              </tr>
            </thead>
            <tbody>
              {customers.map((c) => (
                <tr key={c.id} style={{ borderTop: "1px solid var(--line-soft)" }}>
                  <td className="px-4 py-3">
                    <span className="flex items-center gap-2.5">
                      <span
                        className="flex items-center justify-center text-[11px] font-semibold"
                        style={{
                          width: 28, height: 28, flex: "none", borderRadius: 999,
                          background: "color-mix(in oklab, var(--series-1) 22%, transparent)",
                        }}
                        aria-hidden
                      >
                        {initials(c.name ?? c.phone_e164.slice(-4))}
                      </span>
                      <span className="min-w-0">
                        <span className="block truncate">{c.name ?? "unnamed"}</span>
                        <span className="num block text-[11px]" style={{ color: "var(--faint)" }}>
                          {c.phone_e164}
                        </span>
                      </span>
                      {c.tier === "hot" && <Status tone="warning">hot</Status>}
                    </span>
                  </td>
                  <td className="hidden px-4 py-3 text-[12px] lg:table-cell" style={{ color: "var(--muted)" }}>
                    {Object.keys(c.notes).length === 0
                      ? "—"
                      : Object.entries(c.notes).slice(0, 3).map(([k, v]) => `${k}: ${v}`).join(" · ")}
                  </td>
                  <td className="num hidden px-4 py-3 text-right md:table-cell">{c.conversations}</td>
                  <td className="num px-4 py-3 text-right">{c.orders}</td>
                  <td className="num px-4 py-3 text-right font-medium">
                    {c.spent_kobo > 0 ? naira(c.spent_kobo) : "—"}
                  </td>
                  <td className="num hidden px-4 py-3 text-right md:table-cell" style={{ color: "var(--faint)" }}>
                    {relativeTime(c.last_seen_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </TableWrap>
        )}
      </Card>
    </div>
  );
}
