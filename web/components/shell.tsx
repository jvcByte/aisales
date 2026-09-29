"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { Sidebar } from "./sidebar";
import { TopBar } from "./topbar";
import type { Status } from "../lib/api";

/** The app frame.
 *
 * Three things are load-bearing here and each was a real bug before:
 *
 * 1. `h-[100dvh]` rather than `h-screen`. On mobile Safari and Chrome the URL
 *    bar changes the viewport height as you scroll; `100vh` is the height with
 *    the bar *hidden*, so a `100vh` frame puts the bottom of the page under
 *    the browser chrome where it cannot be reached. `dvh` tracks it.
 *
 * 2. `overflow-hidden` on the frame and `overflow-y-auto` on the content.
 *    Exactly one thing scrolls. The header and sidebar never move, which is
 *    what was asked for and also what stops a long table from scrolling the
 *    navigation off the screen on a phone.
 *
 * 3. The sidebar is a drawer below `lg`. A 224px rail on a 360px screen leaves
 *    136px for the actual work, so the rail is off-canvas there and slides in
 *    over a backdrop.
 */
export function Shell({
  status,
  children,
}: {
  status: Status | null;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();

  // Close on navigation. Without this, tapping a nav item on a phone leaves
  // the drawer covering the page it just opened.
  useEffect(() => setOpen(false), [pathname]);

  // Stop the page behind the drawer from scrolling under it.
  useEffect(() => {
    if (!open) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open]);

  // Escape closes it, which is the only way out on a keyboard.
  useEffect(() => {
    if (!open) return;
    function key(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [open]);

  return (
    <div className="flex h-[100dvh] flex-col overflow-hidden">
      <TopBar
        status={status}
        menuOpen={open}
        onToggleMenu={() => setOpen((v) => !v)}
      />

      <div className="flex min-h-0 flex-1">
        {open && (
          <button
            className="fixed inset-0 z-30 cursor-default lg:hidden"
            style={{ background: "rgba(0,0,0,0.55)" }}
            onClick={() => setOpen(false)}
            aria-label="Close navigation"
            tabIndex={-1}
          />
        )}

        <Sidebar
          attention={status?.attention ?? 0}
          channel={status?.channel ?? "simulator"}
          offline={status?.offline ?? true}
          models={status?.models ?? []}
          open={open}
          onClose={() => setOpen(false)}
        />

        {/* The only scroller in the app. `overscroll-contain` keeps a flick at
            the end of the list from pulling the whole page. */}
        {status?.suspended && (
        <div
          role="status"
          className="flex flex-none flex-wrap items-baseline gap-x-2 gap-y-0.5 px-3 py-2 text-[12px] sm:px-4"
          style={{
            background: "color-mix(in oklab, var(--critical) 12%, var(--panel))",
            borderBottom: "1px solid color-mix(in oklab, var(--critical) 30%, transparent)",
            color: "var(--ink)",
          }}
        >
          <strong className="font-medium">The AI is paused for this shop.</strong>
          <span style={{ color: "var(--muted)" }}>
            Customers&rsquo; messages are still being saved
            {status.suspended_reason ? ` — ${status.suspended_reason}` : ""}
            {" "}— but nobody is answering them. Reply from the inbox yourself.
          </span>
        </div>
      )}

      <main className="min-w-0 flex-1 overflow-y-auto overscroll-contain">
          <div className="p-3 sm:p-4 lg:p-5">{children}</div>
        </main>
      </div>
    </div>
  );
}
