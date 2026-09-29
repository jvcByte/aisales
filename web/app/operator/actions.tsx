"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { type OperatorBusiness, post } from "../../lib/api";

/** Suspend, or resume.
 *
 * One control rather than two, because a business is only ever in one of the
 * two states and offering both would invite the question of what happens when
 * you press the one that does not apply.
 *
 * Suspension stops a business answering its customers, so it asks first. It is
 * the only destructive action on this page and the only one that needs a
 * second click.
 */
export function Actions({ business }: { business: OperatorBusiness }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    if (!business.suspended && !confirm(
      `Suspend ${business.name}? The agent stops answering immediately. ` +
      `Customers' messages are still recorded.`)) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await post(
        `/operator/businesses/${business.id}/${business.suspended ? "resume" : "suspend"}`,
        business.suspended ? {} : { reason: "suspended from the console" });
      startTransition(() => router.refresh());
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="inline-flex items-center gap-2">
      {error && (
        <span className="text-[11px]" style={{ color: "var(--critical)" }} title={error}>
          failed
        </span>
      )}
      <button
        onClick={() => void run()}
        disabled={busy || pending}
        className="btn"
        style={business.suspended
          ? undefined
          : { color: "var(--critical)", borderColor: "color-mix(in oklab, var(--critical) 35%, transparent)" }}
      >
        {busy || pending ? "…" : business.suspended ? "Resume" : "Suspend"}
      </button>
    </span>
  );
}
