"use client";

import { useState } from "react";

// A grouped bar chart, built from divs rather than SVG on purpose: an SVG
// viewBox scales its text along with its geometry, so 11px labels become
// unreadable the moment the card narrows. CSS bars keep real pixels.
//
// The series colours are categorical slots 1-3 of the reference palette,
// validated against this card's own surface (#141a22) for the lightness band,
// chroma floor, CVD separation and 3:1 contrast. They are assigned in fixed
// order and never cycled.

const SERIES = [
  { key: "conversations", label: "Conversations", color: "var(--series-1)" },
  { key: "leads", label: "Leads", color: "var(--series-2)" },
  { key: "orders", label: "Orders", color: "var(--series-3)" },
] as const;

type Point = { day: string; conversations: number; leads: number; orders: number };

/** A round maximum, so the gridlines land on numbers a person can read. */
function niceMax(value: number): number {
  if (value <= 4) return 4;
  const step = Math.pow(10, Math.floor(Math.log10(value)));
  for (const multiple of [1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10]) {
    if (value <= step * multiple) return step * multiple;
  }
  return step * 10;
}

export function GroupedBars({ data, height = 150 }: { data: Point[]; height?: number }) {
  const [hovered, setHovered] = useState<string | null>(null);
  const [asTable, setAsTable] = useState(false);

  const max = niceMax(
    Math.max(1, ...data.flatMap((d) => [d.conversations, d.leads, d.orders])),
  );
  const gridlines = [0, 0.25, 0.5, 0.75, 1];

  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 pb-3">
        {SERIES.map((s) => (
          <span key={s.key} className="flex items-center gap-1.5 text-[11px]"
                style={{ color: "var(--muted)" }}>
            <span aria-hidden style={{ width: 9, height: 9, borderRadius: 2, background: s.color }} />
            {s.label}
          </span>
        ))}
        {/* The relief the palette validator asks for, and the fix for a chart
            with no numbers on it. Aqua is 2.74:1 on the light surface and
            cannot be darkened without breaking colour-blind separation against
            orange, so the values have to be readable another way. */}
        <button
          className="btn ml-auto"
          aria-pressed={asTable}
          onClick={() => setAsTable((v) => !v)}
        >
          {asTable ? "Chart" : "Table"}
        </button>
      </div>

      {asTable && (
        <table className="w-full text-[12px]">
          <thead>
            <tr style={{ color: "var(--muted)" }}>
              <th className="py-1.5 text-left font-normal">Day</th>
              {SERIES.map((s) => (
                <th key={s.key} className="py-1.5 text-right font-normal">{s.label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.map((point) => (
              <tr key={point.day} style={{ borderTop: "1px solid var(--line-soft)" }}>
                <th scope="row" className="py-1.5 text-left font-normal">
                  {new Date(point.day).toLocaleDateString("en-NG",
                    { weekday: "short", day: "numeric", month: "short" })}
                </th>
                {SERIES.map((s) => (
                  <td key={s.key} className="num py-1.5 text-right">{point[s.key]}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {!asTable && (
      <div>

      <div className="flex gap-2">
        {/* Recessive axis: a hairline's worth of ink, never competing with the marks. */}
        <div className="num flex flex-col justify-between text-[10px]"
             style={{ height, color: "var(--faint)" }}>
          {[...gridlines].reverse().map((g) => (
            <span key={g} style={{ transform: "translateY(-50%)" }}>
              {Math.round(max * g)}
            </span>
          ))}
        </div>

        <div className="relative flex-1" style={{ height }}>
          {gridlines.map((g) => (
            <div
              key={g}
              aria-hidden
              className="absolute left-0 right-0"
              style={{
                bottom: `${g * 100}%`,
                borderTop: `1px ${g === 0 ? "solid" : "dashed"} var(--line-soft)`,
              }}
            />
          ))}

          <div className="absolute inset-0 flex items-end justify-between gap-1">
            {data.map((point) => {
              const isHovered = hovered === point.day;
              return (
                <div
                  key={point.day}
                  className="relative flex h-full flex-1 flex-col justify-end"
                  onMouseEnter={() => setHovered(point.day)}
                  onMouseLeave={() => setHovered(null)}
                  onFocus={() => setHovered(point.day)}
                  onBlur={() => setHovered(null)}
                  tabIndex={0}
                  role="img"
                  aria-label={`${point.day}: ${point.conversations} conversations, ${point.leads} leads, ${point.orders} orders`}
                >
                  {isHovered && (
                    <div
                      className="card pointer-events-none absolute bottom-full left-1/2 z-10 mb-1 whitespace-nowrap px-2.5 py-1.5 text-[11px]"
                      style={{
                        transform: "translateX(-50%)",
                        background: "var(--panel-2)",
                        boxShadow: "0 6px 20px rgba(0,0,0,0.5)",
                      }}
                    >
                      {SERIES.map((s) => (
                        <div key={s.key} className="flex items-center gap-2">
                          <span aria-hidden style={{ width: 7, height: 7, borderRadius: 2, background: s.color }} />
                          <span style={{ color: "var(--muted)" }}>{s.label}</span>
                          <span className="num ml-auto font-medium">{point[s.key]}</span>
                        </div>
                      ))}
                    </div>
                  )}

                  {/* 2px between adjacent bars, so two fills never touch. */}
                  <div className="flex h-full items-end justify-center gap-[2px]">
                    {SERIES.map((s) => (
                      <div
                        key={s.key}
                        style={{
                          width: "26%",
                          maxWidth: 9,
                          height: `${(point[s.key] / max) * 100}%`,
                          minHeight: point[s.key] > 0 ? 2 : 0,
                          background: s.color,
                          // Rounded at the data end, square on the baseline.
                          borderRadius: "4px 4px 0 0",
                          opacity: hovered && !isHovered ? 0.45 : 1,
                          transition: "opacity 120ms ease",
                        }}
                      />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <div className="mt-1.5 flex justify-between pl-7">
        {data.map((point) => (
          <span key={point.day} className="flex-1 text-center text-[10px]"
                style={{ color: hovered === point.day ? "var(--ink)" : "var(--faint)" }}>
            {new Date(point.day).toLocaleDateString("en-NG", { weekday: "short" })}
          </span>
        ))}
      </div>
      </div>
      )}

      {/* When the chart is shown, the same numbers are still available to a
          screen reader. When the table is shown, it already is the table. */}
      {!asTable && (
      <table className="sr-only">
        <caption>Activity by day</caption>
        <thead>
          <tr>
            <th>Day</th>
            {SERIES.map((s) => <th key={s.key}>{s.label}</th>)}
          </tr>
        </thead>
        <tbody>
          {data.map((point) => (
            <tr key={point.day}>
              <th scope="row">{point.day}</th>
              {SERIES.map((s) => <td key={s.key}>{point[s.key]}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      )}
    </div>
  );
}
