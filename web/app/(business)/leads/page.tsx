import { Card, Empty, Status, TableWrap } from "../../../components/ui";
import { type Lead, get, relativeTime } from "../../../lib/api";

export const dynamic = "force-dynamic";

export default async function Leads() {
  const { leads } = await get<{ leads: Lead[] }>(`/leads`);
  const hot = leads.filter((l) => l.tier === "hot").length;

  return (
    <div className="space-y-4">
      <header className="flex items-baseline gap-3">
        <h1 className="text-[18px] font-semibold tracking-tight">Leads</h1>
        <span className="text-[12px]" style={{ color: "var(--muted)" }}>
          {leads.length === 0
            ? "none yet — they appear once the AI sees buying intent"
            : hot > 0
              ? `${hot} of ${leads.length} are ready to buy`
              : `${leads.length}, none hot yet`}
        </span>
      </header>

      <Card bodyClassName="p-0">
        {leads.length === 0 ? (
          <Empty>No leads yet.</Empty>
        ) : (
          <TableWrap>
            <thead>
              <tr style={{ color: "var(--muted)" }}>
                <th className="px-4 py-2.5 text-left font-normal">Customer</th>
                <th className="px-4 py-2.5 text-left font-normal">Interest</th>
                <th className="hidden px-4 py-2.5 text-left font-normal md:table-cell">Why</th>
                <th className="hidden px-4 py-2.5 text-right font-normal sm:table-cell">Last seen</th>
              </tr>
            </thead>
            <tbody>
              {leads.map((lead) => (
                <tr key={lead.id} style={{ borderTop: "1px solid var(--line-soft)" }}>
                  <td className="num px-4 py-3">{lead.name ?? lead.phone_e164}</td>
                  <td className="px-4 py-3">
                    {/* `hot` is emphasised and the rest recede. Three coloured
                        tiers would mean none of them carried any signal. */}
                    <span
                      className="text-[12px] uppercase tracking-wide"
                      style={{
                        color: lead.tier === "hot" ? "var(--accent)" : "var(--muted)",
                        fontWeight: lead.tier === "hot" ? 600 : 400,
                      }}
                    >
                      {lead.tier}
                    </span>
                    {lead.intent && (
                      <span className="chip ml-2">{String(lead.intent).replace(/_/g, " ")}</span>
                    )}
                  </td>
                  <td className="hidden px-4 py-3 md:table-cell" style={{ color: "var(--muted)" }}>{lead.reason}</td>
                  <td className="num hidden px-4 py-3 text-right sm:table-cell" style={{ color: "var(--faint)" }}>
                    {relativeTime(lead.updated_at)}
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
