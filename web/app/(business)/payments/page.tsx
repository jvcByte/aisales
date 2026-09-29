import { Card, Empty, Status, TableWrap } from "../../../components/ui";
import { type Payment, get, naira, relativeTime } from "../../../lib/api";

export const dynamic = "force-dynamic";

const TONE: Record<Payment["status"], "good" | "warning" | "critical" | "neutral"> = {
  success: "good",
  pending: "warning",
  failed: "critical",
  abandoned: "neutral",
};

export default async function Payments() {
  const data = await get<{ payments: Payment[]; settled_kobo: number; pending_kobo: number }>(
    `/payments`,
  );
  const { payments, settled_kobo, pending_kobo } = data;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h1 className="text-[18px] font-semibold tracking-tight">Payments</h1>
        <span className="text-[12px]" style={{ color: "var(--muted)" }}>
          {payments.length === 0 ? "none yet" : (
            <>
              <span className="num">{naira(settled_kobo)}</span> settled
              {pending_kobo > 0 && (
                <> · <span className="num" style={{ color: "var(--warning)" }}>{naira(pending_kobo)}</span> still pending</>
              )}
            </>
          )}
        </span>
      </header>

      <div
        className="px-4 py-2.5 text-[12px]"
        style={{
          borderRadius: "var(--radius-md)",
          border: "1px solid color-mix(in oklab, var(--accent) 28%, transparent)",
          background: "color-mix(in oklab, var(--accent) 7%, transparent)",
          color: "var(--muted)",
        }}
      >
        A payment shows as settled only when Paystack says so, and only when the
        amount matches the order. A customer saying they paid changes nothing here.
      </div>

      <Card bodyClassName="p-0">
        {payments.length === 0 ? (
          <Empty>No payments yet.</Empty>
        ) : (
          <TableWrap>
            <thead>
              <tr style={{ color: "var(--muted)" }}>
                <th className="px-4 py-2.5 text-left font-normal">Reference</th>
                <th className="hidden px-4 py-2.5 text-left font-normal md:table-cell">Order</th>
                <th className="px-4 py-2.5 text-left font-normal">Customer</th>
                <th className="hidden px-4 py-2.5 text-left font-normal lg:table-cell">Channel</th>
                <th className="px-4 py-2.5 text-right font-normal">Amount</th>
                <th className="px-4 py-2.5 text-left font-normal">Status</th>
                <th className="hidden px-4 py-2.5 text-right font-normal md:table-cell">Verified</th>
              </tr>
            </thead>
            <tbody>
              {payments.map((p) => (
                <tr key={p.id} style={{ borderTop: "1px solid var(--line-soft)" }}>
                  <td className="num px-4 py-3 text-[12px]">{p.reference}</td>
                  <td className="num hidden px-4 py-3 md:table-cell">{p.order_reference ?? "—"}</td>
                  <td className="num px-4 py-3">{p.name ?? p.phone_e164 ?? "—"}</td>
                  <td className="hidden px-4 py-3 lg:table-cell" style={{ color: "var(--muted)" }}>{p.channel ?? "—"}</td>
                  <td className="num px-4 py-3 text-right font-medium">{naira(p.amount_kobo)}</td>
                  <td className="px-4 py-3"><Status tone={TONE[p.status]}>{p.status}</Status></td>
                  <td className="num hidden px-4 py-3 text-right md:table-cell" style={{ color: "var(--faint)" }}>
                    {p.verified_at ? relativeTime(p.verified_at) : "—"}
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
