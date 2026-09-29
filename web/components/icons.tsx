// A small inline icon set. No dependency: ten 24px paths are less code than
// wiring up an icon library, and it keeps the bundle honest about what it
// actually draws.

const PATHS: Record<string, string> = {
  dashboard: "M3 3h7v7H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 14h7v7H3z",
  conversations: "M21 12a8 8 0 0 1-11.5 7.2L4 21l1.8-5.5A8 8 0 1 1 21 12z",
  leads: "M15 20v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M8.5 10a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM19 8v6M22 11h-6",
  orders: "M21 8 12 3 3 8v8l9 5 9-5zM3 8l9 5 9-5M12 13v8",
  catalogue: "M20.6 13.4 12 22l-9-9V4h9zM7.5 7.5h.01",
  customers: "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8",
  followups: "M21 12a9 9 0 1 1-2.6-6.4M21 3v6h-6",
  payments: "M2 6h20v12H2zM2 10h20",
  reports: "M3 3v18h18M7 15v3M12 9v9M17 5v13",
  settings: "M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2v.2a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.6 1.7 1.7 0 0 0-1.9.4l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0-1.2-2.9H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.7 9a1.7 1.7 0 0 0-.4-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3",
  store: "M3 9 4.5 4h15L21 9M3 9h18v11H3zM3 9a3 3 0 0 0 6 0 3 3 0 0 0 6 0 3 3 0 0 0 6 0",
  shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  spark: "M13 2 4 14h6l-1 8 9-12h-6z",
  chevron: "m6 9 6 6 6-6",
  filter: "M3 5h18l-7 8v6l-4-2v-4z",
  cash: "M12 2v20M17 6.5c0-2-2.2-3.5-5-3.5S7 4.5 7 6.5 9.2 10 12 10s5 1.5 5 3.5-2.2 3.5-5 3.5-5-1.5-5-3.5",
};

export function Icon({ name, size = 16, filled = false }: {
  name: keyof typeof PATHS | string; size?: number; filled?: boolean;
}) {
  const d = PATHS[name];
  if (!d) return null;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill={filled ? "currentColor" : "none"}
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      style={{ flex: "none" }}
    >
      <path d={d} />
    </svg>
  );
}
