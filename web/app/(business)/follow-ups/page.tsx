import { Card, Empty, Status } from "../../../components/ui";
import { type FollowUp, get, relativeTime } from "../../../lib/api";

export const dynamic = "force-dynamic";

const STATUS_TONE: Record<FollowUp["status"], "good" | "warning" | "critical" | "neutral"> = {
  scheduled: "warning",
  sent: "good",
  cancelled: "neutral",
  skipped: "neutral",
};

/** Why a follow-up did not happen, in words.
 *
 * This screen exists to answer "why did nobody chase this customer?", and a
 * list of pending rows alone could not. The codes are the scheduler's own. */
const SKIP_REASON: Record<string, string> = {
  customer_replied: "the customer replied first",
  window_closed: "the 24-hour WhatsApp window had closed",
  human_owns_it: "a person was handling it",
  max_attempts: "already followed up the maximum number of times",
  already_scheduled: "one was already scheduled",
  nothing_to_say: "the AI had nothing useful to add",
  guard_blocked: "the draft was blocked by the safety check",
};

export default async function FollowUps() {
  const { follow_ups: rows } = await get<{ follow_ups: FollowUp[] }>(`/follow-ups`);
  const scheduled = rows.filter((r) => r.status === "scheduled");
  const sent = rows.filter((r) => r.status === "sent");

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h1 className="text-[18px] font-semibold tracking-tight">Follow-ups</h1>
        <span className="text-[12px]" style={{ color: "var(--muted)" }}>
          {rows.length === 0
            ? "none yet — they are scheduled when a customer says they will come back"
            : `${scheduled.length} waiting · ${sent.length} sent`}
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
        WhatsApp only permits a free-form message within 24 hours of the
        customer&apos;s last one. Follow-ups outside that window are skipped and
        recorded rather than sent and refused — which is why the default delay
        is two hours, not tomorrow.
      </div>

      <Card bodyClassName="p-0">
        {rows.length === 0 ? (
          <Empty>Nothing scheduled.</Empty>
        ) : (
          <table className="w-full text-[13px]">
            <thead>
              <tr style={{ color: "var(--muted)" }}>
                <th className="px-4 py-2.5 text-left font-normal">Customer</th>
                <th className="px-4 py-2.5 text-left font-normal">About</th>
                <th className="px-4 py-2.5 text-left font-normal">Status</th>
                <th className="px-4 py-2.5 text-right font-normal">Due</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} style={{ borderTop: "1px solid var(--line-soft)" }}>
                  <td className="num px-4 py-3">{row.name ?? row.phone_e164}</td>
                  <td className="px-4 py-3" style={{ color: "var(--muted)" }}>
                    {row.reason}
                  </td>
                  <td className="px-4 py-3">
                    <Status tone={STATUS_TONE[row.status]}>{row.status}</Status>
                    {row.skipped_reason && (
                      <span className="ml-2 text-[11px]" style={{ color: "var(--faint)" }}>
                        {SKIP_REASON[row.skipped_reason] ?? row.skipped_reason}
                      </span>
                    )}
                  </td>
                  <td className="num px-4 py-3 text-right" style={{ color: "var(--faint)" }}>
                    {relativeTime(row.due_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
