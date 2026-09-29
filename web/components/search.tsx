"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { type SearchResults, naira } from "../lib/api";

const EMPTY: SearchResults = { customers: [], orders: [], products: [] };

/** One box across customers, orders and products.
 *
 * Debounced, and it aborts the in-flight request when a newer keystroke
 * arrives -- without that, a slow response for "la" can land after a fast one
 * for "lace" and replace the right results with the wrong ones.
 */
export function Search() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResults>(EMPTY);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const text = query.trim();
    if (!text) {
      setResults(EMPTY);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    const timer = setTimeout(async () => {
      try {
        const response = await fetch(
          `/api/search?q=${encodeURIComponent(text)}`,
          { signal: controller.signal },
        );
        if (response.ok) setResults(await response.json());
      } catch {
        /* aborted or offline: leave the previous results in place */
      } finally {
        setLoading(false);
      }
    }, 200);

    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query]);

  useEffect(() => {
    function away(event: MouseEvent) {
      if (box.current && !box.current.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, []);

  const total = results.customers.length + results.orders.length + results.products.length;
  const show = open && query.trim().length > 0;

  return (
    <div ref={box} className="relative min-w-0 flex-1 max-w-[520px]">
      <input
        value={query}
        onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        placeholder="Search customers, orders, or products…"
        aria-label="Search"
        className="w-full px-3 py-2 text-[13px]"
        style={{
          borderRadius: "var(--radius-md)",
          border: "1px solid var(--line)",
          background: "var(--panel)",
          color: "var(--ink)",
        }}
      />

      {show && (
        <div
          className="card absolute left-0 right-0 top-full z-20 mt-1 max-h-[380px] overflow-y-auto p-0"
          style={{ background: "var(--panel-2)", boxShadow: "0 12px 32px rgba(0,0,0,0.35)" }}
        >
          {loading && total === 0 && (
            <p className="px-3 py-3 text-[12px]" style={{ color: "var(--muted)" }}>Searching…</p>
          )}
          {!loading && total === 0 && (
            <p className="px-3 py-3 text-[12px]" style={{ color: "var(--muted)" }}>
              Nothing matches “{query.trim()}”.
            </p>
          )}

          {results.customers.length > 0 && (
            <Group label="Customers">
              {results.customers.map((c) => (
                <Row key={c.id} href="/customers" onGo={() => setOpen(false)}
                     left={c.name ?? c.phone_e164} right={c.phone_e164} />
              ))}
            </Group>
          )}
          {results.orders.length > 0 && (
            <Group label="Orders">
              {results.orders.map((o) => (
                <Row key={o.id} href="/orders" onGo={() => setOpen(false)}
                     left={o.reference} right={naira(o.total_kobo)} note={o.status.replace(/_/g, " ")} />
              ))}
            </Group>
          )}
          {results.products.length > 0 && (
            <Group label="Products">
              {results.products.map((p) => (
                <Row key={p.id} href="/catalogue" onGo={() => setOpen(false)}
                     left={p.name} right={naira(p.price_kobo)}
                     note={p.stock_qty === null ? "stock not tracked"
                           : p.stock_qty === 0 ? "none left" : `${p.stock_qty} left`} />
              ))}
            </Group>
          )}
        </div>
      )}
    </div>
  );
}

function Group({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="px-3 pt-3 pb-1 text-[10px] uppercase tracking-wide"
           style={{ color: "var(--faint)" }}>
        {label}
      </div>
      <ul>{children}</ul>
    </div>
  );
}

function Row({ href, left, right, note, onGo }: {
  href: string; left: string; right: string; note?: string; onGo: () => void;
}) {
  return (
    <li>
      <Link href={href} onClick={onGo}
            className="flex items-center gap-2 px-3 py-2 text-[12px] hover:opacity-80">
        <span className="truncate">{left}</span>
        {note && <span className="chip">{note}</span>}
        <span className="num ml-auto" style={{ color: "var(--muted)" }}>{right}</span>
      </Link>
    </li>
  );
}
