import { Card, Empty, Status as Chip, TableWrap } from "../../components/ui";
import { type OperatorBusiness, get } from "../../lib/api";

import { Actions } from "./actions";
import { Onboard } from "./onboard";

export const dynamic = "force-dynamic";

/** Every business on the deployment.
 *
 * A table rather than cards, because the question this screen answers is
 * comparative -- which of these is quiet, which is not connected, which is
 * suspended -- and a grid of cards makes you read four panels to answer it.
 * The eye should go down one column, so the columns are the things you would
 * scan by: name, state, connections, activity.
 */
export default async function OperatorConsole() {
  const { businesses } = await get<{ businesses: OperatorBusiness[] }>("/operator/businesses");

  const suspended = businesses.filter((b) => b.suspended).length;
  const connected = businesses.filter((b) => b.integrations.whatsapp).length;
  const messages = businesses.reduce((total, b) => total + b.messages_30d, 0);

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="text-[18px] font-semibold tracking-tight">Businesses</h1>
        <span className="text-[12px]" style={{ color: "var(--muted)" }}>
          {businesses.length === 0
            ? "none yet"
            : `${businesses.length} onboarded · ${connected} on WhatsApp · ` +
              `${messages.toLocaleString("en-NG")} messages in 30 days` +
              (suspended ? ` · ${suspended} suspended` : "")}
        </span>
      </header>

      <Card bodyClassName="p-0">
        {businesses.length === 0 ? (
          <Empty>No businesses yet. Onboard the first one below.</Empty>
        ) : (
          <TableWrap min={720}>
            <thead>
              <tr style={{ color: "var(--muted)" }}>
                <th className="px-4 py-2.5 text-left font-normal">Business</th>
                <th className="px-4 py-2.5 text-left font-normal">State</th>
                <th className="px-4 py-2.5 text-left font-normal">Connections</th>
                <th className="px-4 py-2.5 text-right font-normal">Messages</th>
                <th className="hidden px-4 py-2.5 text-right font-normal sm:table-cell">
                  Leads
                </th>
                <th className="hidden px-4 py-2.5 text-right font-normal md:table-cell">
                  Open
                </th>
                <th className="px-4 py-2.5 text-right font-normal">Actions</th>
              </tr>
            </thead>
            <tbody>
              {businesses.map((business) => (
                <tr key={business.id} style={{ borderTop: "1px solid var(--line-soft)" }}>
                  <td className="px-4 py-3">
                    <span className="block font-medium">{business.name}</span>
                    <span className="num block text-[11px]" style={{ color: "var(--faint)" }}>
                      {business.slug}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    {business.suspended ? (
                      <Chip tone="critical">Suspended</Chip>
                    ) : (
                      <Chip tone="good">Live</Chip>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <span className="flex flex-wrap gap-1.5">
                      <Connection label="WhatsApp" on={Boolean(business.integrations.whatsapp)} />
                      <Connection label="Paystack" on={Boolean(business.integrations.paystack)} />
                    </span>
                  </td>
                  <td className="num px-4 py-3 text-right">{business.messages_30d}</td>
                  <td className="num hidden px-4 py-3 text-right sm:table-cell">
                    {business.leads}
                  </td>
                  <td className="num hidden px-4 py-3 text-right md:table-cell">
                    {business.open_threads}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <Actions business={business} />
                  </td>
                </tr>
              ))}
            </tbody>
          </TableWrap>
        )}
      </Card>

      {suspended > 0 && (
        <p className="text-[12px]" style={{ color: "var(--muted)" }}>
          A suspended business still records what customers send — nothing is lost —
          but the agent does not answer and scheduled follow-ups are cancelled.
          Resuming catches up any thread still inside WhatsApp&rsquo;s 24-hour window;
          older ones are left for a person.
        </p>
      )}

      <Onboard />
    </div>
  );
}

/** A connection is a fact, not a score: present or absent. */
function Connection({ label, on }: { label: string; on: boolean }) {
  return (
    <span
      className="chip"
      style={on ? undefined : { color: "var(--faint)", borderStyle: "dashed" }}
      title={on ? `${label} connected` : `${label} not connected`}
    >
      <span
        aria-hidden
        style={{
          width: 6, height: 6, borderRadius: 999,
          background: on ? "var(--good)" : "var(--faint)",
        }}
      />
      {label}
    </span>
  );
}
