import { SettingsForm } from "./form";
import { get } from "../../../lib/api";

export const dynamic = "force-dynamic";

type Payload = { id: string; slug: string; name: string; settings: Record<string, unknown> };

export default async function Settings() {
  const data = await get<Payload>(`/settings`);
  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h1 className="text-[18px] font-semibold tracking-tight">Settings</h1>
        <span className="text-[12px]" style={{ color: "var(--muted)" }}>
          what the AI is allowed to say and do
        </span>
      </header>
      <SettingsForm slug={data.slug} business={data.name} settings={data.settings} />
    </div>
  );
}
