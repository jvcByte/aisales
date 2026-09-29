"use client";

/** The console's own failure state.
 *
 * The business dashboard's says "The dashboard could not reach the API", which
 * is the wrong sentence on a page that reads across every business -- and the
 * commonest reason this page fails is not the API being down but the *operator
 * connection* not being configured. Naming that difference is the whole value
 * of having a separate boundary.
 */
export default function OperatorError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const notConfigured = error.message.includes("AISALES_ADMIN_DSN");

  return (
    <div className="card p-6" style={{ maxWidth: 560 }}>
      <p className="text-[14px] font-medium">
        {notConfigured
          ? "The operator console is not configured."
          : "The console could not reach the API."}
      </p>
      <p className="mt-1 text-[12px]" style={{ color: "var(--muted)" }}>
        {error.message}
      </p>
      {notConfigured && (
        <p className="mt-3 text-[12px]" style={{ color: "var(--muted)" }}>
          Set <code className="num">AISALES_ADMIN_DSN</code> to a DSN for a role
          that is a member of the operator role, then restart the API. A
          deployment running a single business is expected not to have one.
        </p>
      )}
      <button
        onClick={reset}
        className="mt-4 px-3 py-1.5 text-[12px]"
        style={{
          borderRadius: "var(--radius-md)",
          background: "var(--accent)",
          color: "var(--accent-ink)",
        }}
      >
        Try again
      </button>
    </div>
  );
}
