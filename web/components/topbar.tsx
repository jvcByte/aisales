import Link from "next/link";
import { useState } from "react";
import { Icon } from "./icons";
import { Search } from "./search";
import { ThemeToggle } from "./theme-toggle";
import { type Status } from "../lib/api";

/** The header. Fixed height, never scrolls.
 *
 * On a phone it becomes two rows: identity and controls, then the search on
 * its own full-width line. Squeezing all four groups onto one 360px line means
 * either the search is unusably narrow or the whole bar wraps unpredictably.
 */
export function TopBar({
  status,
  menuOpen,
  onToggleMenu,
}: {
  status: Status | null;
  menuOpen: boolean;
  onToggleMenu: () => void;
}) {
  const owner = status?.owner;

  return (
    <header
      className="flex flex-none flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2.5 sm:px-4 lg:flex-nowrap lg:py-3"
      style={{ background: "var(--panel-2)", borderBottom: "1px solid var(--line)" }}
    >
      <button
        onClick={onToggleMenu}
        aria-expanded={menuOpen}
        aria-label={menuOpen ? "Close navigation" : "Open navigation"}
        className="btn lg:hidden"
        style={{ padding: "6px 8px" }}
      >
        <span className="flex flex-col gap-[3px]" aria-hidden>
          <span style={{ width: 15, height: 2, background: "currentColor", borderRadius: 2 }} />
          <span style={{ width: 15, height: 2, background: "currentColor", borderRadius: 2 }} />
          <span style={{ width: 15, height: 2, background: "currentColor", borderRadius: 2 }} />
        </span>
      </button>

      <Link href="/" className="flex min-w-0 items-center gap-2.5">
        <span
          className="flex flex-none items-center justify-center"
          style={{
            width: 30, height: 30, borderRadius: "var(--radius-md)",
            background: "var(--accent)", color: "var(--accent-ink)",
          }}
          aria-hidden
        >
          <Icon name="spark" size={16} filled />
        </span>
        <span className="min-w-0 leading-tight">
          <span className="block truncate text-[13px] font-semibold tracking-tight sm:text-[14px]">
            AI Sales Employee
          </span>
          {/* First thing to go when space runs out: it is the least
              load-bearing text on the screen. */}
          <span className="hidden text-[10px] sm:block" style={{ color: "var(--muted)" }}>
            WhatsApp · Sell · Follow Up · Grow
          </span>
        </span>
      </Link>

      {/* Own row on phones, inline from `lg`. */}
      <div className="order-last w-full min-w-0 sm:order-none sm:w-auto sm:flex-1 sm:px-2 lg:order-none">
        <div className="mx-auto w-full max-w-[520px]">
          <Search />
        </div>
      </div>

      <div className="ml-auto flex items-center gap-2 sm:gap-3">
        <div className="flex items-center gap-2">
          <span className="hidden sm:flex" style={{ color: "var(--accent)" }}>
            <Icon name="store" size={18} />
          </span>
          <span className="hidden leading-tight md:block">
            <span className="block max-w-[130px] truncate text-[12px] font-medium">
              {status?.business ?? "—"}
            </span>
            <span className="block text-[10px]" style={{ color: "var(--muted)" }}>
              {owner?.configured ? "Owner" : "Not set in Settings"}
            </span>
          </span>
        </div>

        <Account status={status} initials={owner?.initials ?? "?"} />

        <ThemeToggle />
      </div>
    </header>
  );
}


/** The avatar, and the only place a session can be ended.
 *
 * Signing out is deliberately not a nav item: it is not somewhere you go, it
 * is something you do to the session you are in, and burying it in the sidebar
 * next to "Reports" invites the misclick that costs somebody their afternoon.
 */
function Account({ status, initials }: { status: Status | null; initials: string }) {
  const [open, setOpen] = useState(false);
  const who = status?.user;

  async function signOut() {
    await fetch("/api/auth/sign-out", { method: "POST" }).catch(() => null);
    // A full navigation, so no server component keeps rendering the shop the
    // session no longer has access to.
    window.location.href = "/sign-in";
  }

  return (
    <div className="relative flex-none">
      <button
        onClick={() => setOpen((was) => !was)}
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label="Account"
        className="flex items-center justify-center text-[11px] font-semibold sm:text-[12px]"
        style={{
          width: 28, height: 28, borderRadius: 999, border: "1px solid var(--line)",
          background: "color-mix(in oklab, var(--series-1) 26%, transparent)",
          color: "var(--ink)", cursor: "pointer",
        }}
        title={who?.name ?? who?.email}
      >
        {initials}
      </button>

      {open && (
        <>
          {/* Catches the click that means "close this". Cheaper and more
              reliable than a document listener that has to be torn down. */}
          <button
            aria-hidden
            tabIndex={-1}
            onClick={() => setOpen(false)}
            className="fixed inset-0 z-40 cursor-default"
            style={{ background: "transparent" }}
          />
          <div
            role="menu"
            className="absolute right-0 z-50 mt-1.5 w-[210px] overflow-hidden py-1"
            style={{
              top: "100%",
              background: "var(--panel)",
              border: "1px solid var(--line)",
              borderRadius: "var(--radius-md)",
              // One of the three surfaces that genuinely layers above the
              // page, so it is one of the three that earns a shadow.
              boxShadow: "0 8px 24px rgb(0 0 0 / 0.18)",
            }}
          >
            <div className="px-3 py-2">
              <div className="truncate text-[12px] font-medium">
                {who?.name ?? "Signed in"}
              </div>
              <div className="truncate text-[11px]" style={{ color: "var(--muted)" }}>
                {who?.email}
              </div>
              {status && (
                <div className="mt-1 truncate text-[11px]" style={{ color: "var(--faint)" }}>
                  {status.business} · {who?.role}
                </div>
              )}
            </div>
            <button
              role="menuitem"
              onClick={() => void signOut()}
              className="w-full px-3 py-2 text-left text-[12px]"
              style={{ borderTop: "1px solid var(--line-soft)", cursor: "pointer" }}
            >
              Sign out
            </button>
          </div>
        </>
      )}
    </div>
  );
}
