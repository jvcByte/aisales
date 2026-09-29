import type { ReactNode } from "react";

// Shared primitives. Kept in one file because they are small and always change
// together, and because a component-per-file here would be more files than
// decisions.

export function Card({
  title,
  action,
  children,
  className = "",
  bodyClassName = "p-4",
}: {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={`card flex min-w-0 flex-col ${className}`}>
      {title && (
        <header
          className="flex items-center justify-between gap-3 px-4 py-3"
          style={{ borderBottom: "1px solid var(--line-soft)" }}
        >
          <h2 className="text-[13px] font-medium">{title}</h2>
          {action}
        </header>
      )}
      <div className={`flex-1 ${bodyClassName}`}>{children}</div>
    </section>
  );
}

/** A status chip. The dot is a second channel beside the word, so the meaning
 *  survives colour-blindness, greyscale printing and forced-colors mode. */
export function Status({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "good" | "warning" | "critical";
}) {
  const colour =
    tone === "good" ? "var(--good)"
    : tone === "warning" ? "var(--warning)"
    : tone === "critical" ? "var(--critical)"
    : "var(--faint)";
  return (
    <span className="chip" data-tone={tone}>
      <span
        aria-hidden
        style={{ width: 6, height: 6, borderRadius: 99, background: colour, flex: "none" }}
      />
      {children}
    </span>
  );
}

/** A figure, its label, and a comparison whose period is named in words.
 *  "+18%" on its own is not information -- eighteen percent against what? */
export function Stat({
  label,
  value,
  delta,
  sub,
  primary = false,
}: {
  label: string;
  value: ReactNode;
  delta?: number | null;
  sub?: string;
  primary?: boolean;
}) {
  return (
    <div
      className={primary ? "px-5 py-4" : "px-4 py-3"}
      style={primary ? { background: "color-mix(in oklab, var(--accent) 7%, transparent)" } : undefined}
    >
      <div className="text-[12px]" style={{ color: "var(--muted)" }}>
        {label}
      </div>
      <div
        className={`num mt-1 font-semibold tracking-tight ${
          primary ? "text-[34px] leading-none" : "text-[22px] leading-none"
        }`}
      >
        {value}
      </div>
      <div className="mt-1.5 text-[11px]" style={{ color: "var(--muted)" }}>
        {delta !== undefined && delta !== null && (
          <span style={{ color: delta > 0 ? "var(--good)" : delta < 0 ? "var(--critical)" : undefined }}>
            {delta > 0 ? "↑" : delta < 0 ? "↓" : "—"} {Math.abs(delta)}%{" "}
          </span>
        )}
        {sub}
      </div>
    </div>
  );
}

/** A table that survives a phone.
 *
 * Horizontal scrolling is the fallback, not the plan: the low-priority columns
 * are hidden below `md` so the remaining ones fit, and the scroll only appears
 * if what is left still does not. A table you have to drag sideways to read is
 * worse than one that shows fewer columns -- the numbers you came for should
 * be visible without a gesture nobody discovers.
 */
export function TableWrap({ children, min = 0 }: { children: ReactNode; min?: number }) {
  return (
    <div className="min-w-0 overflow-x-auto">
      <table
        className="w-full text-[13px]"
        style={min ? { minWidth: min } : undefined}
      >
        {children}
      </table>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="px-4 py-6 text-center text-[13px]" style={{ color: "var(--muted)" }}>
      {children}
    </p>
  );
}

/** An icon tile. Decorative washes in four colours are what makes a dashboard
 *  read as generated, so these stay monochrome: the shape carries the identity
 *  and the colour is reserved for state. */
export function Tile({ children }: { children: ReactNode }) {
  return (
    <span
      className="flex items-center justify-center text-[13px]"
      style={{
        width: 28, height: 28, flex: "none",
        borderRadius: "var(--radius-md)",
        background: "var(--line-soft)",
        color: "var(--muted)",
      }}
      aria-hidden
    >
      {children}
    </span>
  );
}
