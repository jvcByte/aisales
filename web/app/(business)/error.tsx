"use client";

// Reserved for a genuinely dead API -- the one case where nothing in the
// content region can render. The sidebar is a sibling of this, so navigation
// survives; anything narrower is caught by a boundary on the region that failed.
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className="card p-6">
      <p className="text-[14px] font-medium">The dashboard could not reach the API.</p>
      <p className="mt-1 text-[13px]" style={{ color: "var(--muted)" }}>{error.message}</p>
      <p className="mt-3 text-[13px]" style={{ color: "var(--muted)" }}>
        Start it with <code className="num">python -m aisales_api</code>, then try again.
      </p>
      <button
        onClick={reset}
        className="mt-4 px-3 py-1.5 text-[13px] font-medium"
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
