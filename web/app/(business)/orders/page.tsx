import { Card, Empty, Status, TableWrap } from "../../../components/ui";
import { type Order, get, naira, relativeTime } from "../../../lib/api";

export const dynamic = "force-dynamic";

function orderTone(status: string): "good" | "warning" | "critical" | "neutral" {
  if (status === "paid" || status === "fulfilled") return "good";
  if (status === "cancelled") return "critical";
  if (status === "awaiting_payment") return "warning";
  return "neutral";
}

export default async function Orders() {
  const { orders } = await get<{ orders: Order[] }>(`/orders`);

  const awaiting = orders.filter((o) => o.status === "awaiting_payment");
  const outstanding = awaiting.reduce((sum, o) => sum + o.total_kobo, 0);
  const collected = orders
    .filter((o) => o.status === "paid" || o.status === "fulfilled")
    .reduce((sum, o) => sum + o.total_kobo, 0);

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h1 className="text-[18px] font-semibold tracking-tight">Orders</h1>
        <span className="text-[12px]" style={{ color: "var(--muted)" }}>
          {orders.length === 0 ? "none yet" : (
            <>
              <span className="num">{naira(collected)}</span> collected
              {outstanding > 0 && (
                <>
                  {" · "}
                  <span className="num" style={{ color: "var(--warning)" }}>
                    {naira(outstanding)}
                  </span>{" "}
                  still owed across {awaiting.length}
                </>
              )}
            </>
          )}
        </span>
      </header>

      <Card bodyClassName="p-0">
        {orders.length === 0 ? (
          <Empty>No orders yet.</Empty>
        ) : (
          <TableWrap>
            <thead>
              <tr style={{ color: "var(--muted)" }}>
                <th className="px-4 py-2.5 text-left font-normal">Reference</th>
                <th className="hidden px-4 py-2.5 text-left font-normal sm:table-cell">Customer</th>
                <th className="hidden px-4 py-2.5 text-right font-normal md:table-cell">Items</th>
                <th className="px-4 py-2.5 text-right font-normal">Total</th>
                <th className="px-4 py-2.5 text-left font-normal">Status</th>
                <th className="hidden px-4 py-2.5 text-right font-normal md:table-cell">Created</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => (
                <tr key={o.id} style={{ borderTop: "1px solid var(--line-soft)" }}>
                  <td className="num px-4 py-3">{o.reference}</td>
                  <td className="num hidden px-4 py-3 sm:table-cell">{o.name ?? o.phone_e164}</td>
                  <td className="num hidden px-4 py-3 text-right md:table-cell">{o.items}</td>
                  <td className="num px-4 py-3 text-right font-medium">{naira(o.total_kobo)}</td>
                  <td className="px-4 py-3">
                    <Status tone={orderTone(o.status)}>{o.status.replace(/_/g, " ")}</Status>
                  </td>
                  <td className="num hidden px-4 py-3 text-right md:table-cell" style={{ color: "var(--faint)" }}>
                    {relativeTime(o.created_at)}
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
