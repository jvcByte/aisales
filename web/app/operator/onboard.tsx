"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Card } from "../../components/ui";
import { post } from "../../lib/api";

/** Onboard a business.
 *
 * Collapsed until asked for. Creating a business is a deliberate act performed
 * once per customer, and an always-open form of nine fields would be the
 * loudest thing on a page whose job is to show you the state of the estate.
 *
 * The credentials section is separate from the identity section because they
 * are genuinely different decisions: a business can exist perfectly well
 * before its WhatsApp number is connected, and every business does for at
 * least a little while.
 */
export function Onboard() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setDone(null);
    const form = new FormData(event.currentTarget);
    const payload = Object.fromEntries(
      [...form.entries()].filter(([, value]) => String(value).trim() !== ""));

    try {
      const created = await post<{ slug: string; owner: string }>(
        "/operator/businesses", payload);
      setDone(`${created.slug} created — ${created.owner} can sign in now.`);
      event.currentTarget.reset();
      router.refresh();
    } catch (problem) {
      // The API's messages are written to be read: "already exists", "these
      // settings cannot be changed here". Passing them through beats inventing
      // a generic one that says less.
      setError(problem instanceof Error ? problem.message : "failed");
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <div className="flex items-center gap-3">
        <button className="btn" onClick={() => setOpen(true)}>Onboard a business</button>
        {done && <span className="text-[12px]" style={{ color: "var(--good)" }}>{done}</span>}
      </div>
    );
  }

  return (
    <Card title="Onboard a business"
          action={<button className="btn" onClick={() => setOpen(false)}>Cancel</button>}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field name="name" label="Business name" required placeholder="Ada Fabrics" />
          <Field name="slug" label="Slug" required placeholder="adafabrics"
                 hint="Lowercase, used in URLs. Cannot be changed later." />
          <Field name="owner_name" label="Owner's name" placeholder="Adaeze Okonkwo" />
          <Field name="owner_email" label="Owner's email" type="email" required
                 placeholder="owner@adafabrics.ng"
                 hint="This is the sign-in they will use." />
          <Field name="owner_password" label="Temporary password" type="password" required
                 hint="At least 12 characters. They should change it." />
        </div>

        {/* `role="group"` rather than `<fieldset>`+`<legend>`: a legend is
            lifted out of its parent's flow by the browser, so it ignores the
            flex gap and ends up sitting on top of the first field's label,
            reading as part of it rather than as the heading of the group. */}
        <div role="group" aria-labelledby="connections-heading"
             className="flex flex-col gap-3">
          <p id="connections-heading" className="text-[12px] font-medium"
             style={{ color: "var(--muted)" }}>
            Connections — optional, and can be added later
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field name="whatsapp_number_id" label="WhatsApp phone number ID"
                   placeholder="123456789012345" />
            <Field name="whatsapp_token" label="WhatsApp access token" type="password" />
            <Field name="whatsapp_app_secret" label="WhatsApp app secret" type="password" />
            <Field name="paystack_account" label="Paystack account code"
                   placeholder="ACCT_xxxxxxxx" />
            <Field name="paystack_secret_key" label="Paystack secret key" type="password" />
          </div>
          <p className="text-[11px]" style={{ color: "var(--muted)" }}>
            Credentials are encrypted before they are stored, and the business
            never sees them. Leave them blank to onboard first and connect later.
          </p>
        </div>

        {error && (
          <p role="alert" className="text-[12px]" style={{ color: "var(--critical)" }}>
            {error}
          </p>
        )}

        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={busy}
            className="px-4 py-2 text-[13px] font-medium disabled:opacity-50"
            style={{
              borderRadius: "var(--radius-md)",
              background: "var(--accent)", color: "var(--accent-ink)",
            }}
          >
            {busy ? "Creating…" : "Create business"}
          </button>
          {done && <span className="text-[12px]" style={{ color: "var(--good)" }}>{done}</span>}
        </div>
      </form>
    </Card>
  );
}

function Field({ name, label, hint, ...rest }: {
  name: string; label: string; hint?: string;
} & React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[11px] font-medium" style={{ color: "var(--muted)" }}>{label}</span>
      <input name={name} className="inp" {...rest} />
      {hint && <span className="text-[10px]" style={{ color: "var(--faint)" }}>{hint}</span>}
    </label>
  );
}
