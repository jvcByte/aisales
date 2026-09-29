"use client";

import { useState } from "react";
import { Card } from "../../../components/ui";

type FollowUp = {
  first_after_minutes?: number;
  max_attempts?: number;
  min_gap_minutes?: number;
  quiet_start?: string;
  quiet_end?: string;
  tone?: string;
};

const ESCALATE = [
  ["refund_request", "Refunds"],
  ["complaint", "Complaints"],
  ["payment_dispute", "Payment disputes"],
  ["delivery_dispute", "Delivery disputes"],
] as const;

const FLAG = [
  ["discount_request", "Discount requests"],
  ["price_objection", "Price objections"],
] as const;

export function SettingsForm({
  slug,
  business,
  settings,
}: {
  slug: string;
  business: string;
  settings: Record<string, unknown>;
}) {
  const follow = (settings.followup as FollowUp) ?? {};
  const [ownerName, setOwnerName] = useState(String(settings.owner_name ?? ""));
  const [tone, setTone] = useState(String(settings.tone ?? ""));
  const [facts, setFacts] = useState(String(settings.approved_facts ?? ""));
  const [privacy, setPrivacy] = useState(String(settings.privacy_notice ?? ""));
  const [threshold, setThreshold] = useState(Number(settings.confidence_threshold ?? 0.6));
  const [firstAfter, setFirstAfter] = useState(follow.first_after_minutes ?? 120);
  const [maxAttempts, setMaxAttempts] = useState(follow.max_attempts ?? 2);
  const [quietStart, setQuietStart] = useState(follow.quiet_start ?? "21:00");
  const [quietEnd, setQuietEnd] = useState(follow.quiet_end ?? "08:00");
  const [escalate, setEscalate] = useState<string[]>(
    (settings.escalate_on as string[]) ?? ["refund_request", "complaint", "payment_dispute", "delivery_dispute"],
  );
  const [flag, setFlag] = useState<string[]>(
    (settings.flag_on as string[]) ?? ["discount_request", "price_objection"],
  );
  const [state, setState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [message, setMessage] = useState("");

  function toggle(list: string[], set: (v: string[]) => void, key: string) {
    set(list.includes(key) ? list.filter((k) => k !== key) : [...list, key]);
  }

  async function save() {
    setState("saving");
    setMessage("");
    try {
      const response = await fetch(`/api/settings?slug=${slug}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          owner_name: ownerName,
          tone,
          approved_facts: facts,
          privacy_notice: privacy,
          confidence_threshold: threshold,
          escalate_on: escalate,
          flag_on: flag,
          followup: {
            first_after_minutes: firstAfter,
            max_attempts: maxAttempts,
            min_gap_minutes: follow.first_after_minutes ?? 1440,
            quiet_start: quietStart,
            quiet_end: quietEnd,
            tone: follow.tone ?? "warm, brief",
          },
        }),
      });
      if (!response.ok) throw new Error((await response.text()) || `${response.status}`);
      setState("saved");
      setMessage("Saved. The next turn uses these.");
    } catch (error) {
      setState("error");
      setMessage(error instanceof Error ? error.message : "could not save");
    }
  }

  return (
    <div className="grid min-w-0 gap-4 xl:grid-cols-[1fr_320px]">
      <div className="min-w-0 space-y-4">
        <Card title="The shop">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Business" hint="Read-only here; set when the shop is created.">
              <input value={business} readOnly className="inp" style={{ opacity: 0.65 }} />
            </Field>
            <Field label="Owner's name" hint="Used in the greeting. Blank means no name is shown.">
              <input value={ownerName} onChange={(e) => setOwnerName(e.target.value)}
                     placeholder="Ada" className="inp" />
            </Field>
          </div>
          <Field label="Tone the AI writes in" hint="A shop assistant who suddenly sounds like a different person is worse than a slightly formal one.">
            <input value={tone} onChange={(e) => setTone(e.target.value)}
                   placeholder="warm, brief, Nigerian English" className="inp" />
          </Field>
        </Card>

        <Card title="Approved business facts">
          <p className="mb-2 text-[12px]" style={{ color: "var(--muted)" }}>
            The only policies the AI may state — delivery, returns, payment, anything with a
            figure in it. Anything not written here, it says it will confirm. Money figures in
            this text become quotable, so a delivery fee belongs here.
          </p>
          <textarea value={facts} onChange={(e) => setFacts(e.target.value)} rows={6}
                    className="inp" style={{ resize: "vertical" }}
                    placeholder="Delivery within Lagos is ₦2,500 and takes 1-2 working days." />
        </Card>

        <Card title="Privacy notice">
          <p className="mb-2 text-[12px]" style={{ color: "var(--muted)" }}>
            Sent verbatim if a customer asks what happens to their data. The AI will not
            improvise one, because an invented data policy is a commitment you did not make.
            Messages reach model providers outside Nigeria — see PRIVACY.md.
          </p>
          <textarea value={privacy} onChange={(e) => setPrivacy(e.target.value)} rows={4}
                    className="inp" style={{ resize: "vertical" }} />
        </Card>
      </div>

      <div className="space-y-4">
        <Card title="When a person takes over">
          <p className="mb-3 text-[12px]" style={{ color: "var(--muted)" }}>
            Handover stops the AI until someone hands the thread back.
          </p>
          {ESCALATE.map(([key, label]) => (
            <Toggle key={key} label={label} checked={escalate.includes(key)}
                    onChange={() => toggle(escalate, setEscalate, key)} />
          ))}
          <p className="mt-3 mb-2 text-[12px]" style={{ color: "var(--muted)" }}>
            These only flag — the AI keeps selling while you look.
          </p>
          {FLAG.map(([key, label]) => (
            <Toggle key={key} label={label} checked={flag.includes(key)}
                    onChange={() => toggle(flag, setFlag, key)} />
          ))}
          <p className="mt-3 text-[11px]" style={{ color: "var(--faint)" }}>
            A discount request flags rather than hands over on purpose: “Reduce am” is the most
            common opening move in Nigerian commerce, and stopping the AI every time would take
            it offline for most customers. It cannot grant one either way.
          </p>
        </Card>

        <Card title="Follow-ups">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="First, after (minutes)"
                   hint="Under 24h on purpose: WhatsApp only allows a free-form message inside the customer service window.">
              <input type="number" value={firstAfter} min={5}
                     onChange={(e) => setFirstAfter(Number(e.target.value))} className="inp num" />
            </Field>
            <Field label="At most" hint="Attempts per conversation.">
              <input type="number" value={maxAttempts} min={0} max={5}
                     onChange={(e) => setMaxAttempts(Number(e.target.value))} className="inp num" />
            </Field>
            <Field label="Quiet from" hint="Lagos time.">
              <input value={quietStart} onChange={(e) => setQuietStart(e.target.value)} className="inp num" />
            </Field>
            <Field label="Quiet until">
              <input value={quietEnd} onChange={(e) => setQuietEnd(e.target.value)} className="inp num" />
            </Field>
          </div>
        </Card>

        <Card title="Escalate when unsure">
          <Field label={`Confidence threshold: ${threshold.toFixed(2)}`}
                 hint="Below this, and if the reply commits to a price, a person is asked to look.">
            <input type="range" min={0} max={1} step={0.05} value={threshold}
                   onChange={(e) => setThreshold(Number(e.target.value))} className="w-full" />
          </Field>
        </Card>

        <div className="flex items-center gap-3">
          <button onClick={() => void save()} disabled={state === "saving"}
                  className="px-4 py-2 text-[13px] font-medium disabled:opacity-50"
                  style={{ borderRadius: "var(--radius-md)", background: "var(--accent)", color: "var(--accent-ink)" }}>
            {state === "saving" ? "Saving…" : "Save settings"}
          </button>
          {message && (
            <span className="text-[12px]"
                  style={{ color: state === "error" ? "var(--critical)" : "var(--muted)" }}>
              {message}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

function Field({ label, hint, children }: {
  label: string; hint?: string; children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[12px]" style={{ color: "var(--muted)" }}>{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11px]" style={{ color: "var(--faint)" }}>{hint}</span>}
    </label>
  );
}

function Toggle({ label, checked, onChange }: {
  label: string; checked: boolean; onChange: () => void;
}) {
  return (
    <label className="flex items-center gap-2.5 py-1 text-[13px]">
      <input type="checkbox" checked={checked} onChange={onChange} />
      {label}
    </label>
  );
}
