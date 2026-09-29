import Link from "next/link";
import { redirect } from "next/navigation";

import { Icon } from "../../components/icons";
import { ThemeToggle } from "../../components/theme-toggle";
import { type Principal, peek } from "../../lib/api";

/** The console's own chrome.
 *
 * Deliberately not the business `Shell`. That one is a shop's instrument -- a
 * conversation inbox, a lead count, a payments view -- and every item in it
 * belongs to one business. An operator looking at every business at once is
 * doing a different job, and wrapping it in one shop's sidebar would suggest
 * the numbers beside it were that shop's.
 */
export default async function OperatorLayout({ children }: { children: React.ReactNode }) {
  const who = await peek<{ user: Principal }>("/auth/me");
  if (!who) redirect("/sign-in");
  // Not a 403 page: somebody who is merely signed in as a shop owner should
  // land where they can actually work, not on an error they cannot act on.
  if (!who.user.is_operator) redirect("/");

  return (
    <div className="flex min-h-dvh flex-col">
      <header
        className="flex flex-none items-center gap-3 px-4 py-3"
        style={{ background: "var(--panel-2)", borderBottom: "1px solid var(--line)" }}
      >
        <Link href="/operator" className="flex min-w-0 items-center gap-2.5">
          <span
            className="flex flex-none items-center justify-center"
            style={{
              width: 28, height: 28, borderRadius: "var(--radius-md)",
              background: "var(--ink)", color: "var(--panel)",
            }}
            aria-hidden
          >
            <Icon name="shield" size={15} />
          </span>
          <span className="min-w-0 leading-tight">
            <span className="block truncate text-[13px] font-semibold tracking-tight">
              Operator console
            </span>
            <span className="block truncate text-[10px]" style={{ color: "var(--muted)" }}>
              {who.user.email}
            </span>
          </span>
        </Link>

        <div className="ml-auto flex items-center gap-2">
          {/* Only for an operator who is also a member of a business. For one
              who is not, it led to a dashboard that cannot load -- a link to
              nowhere is worse than no link. */}
          {who.user.business && (
            <Link href="/" className="btn">My dashboard</Link>
          )}
          <ThemeToggle />
        </div>
      </header>

      <main className="min-w-0 flex-1 overflow-y-auto overscroll-contain">
        <div className="p-3 sm:p-4 lg:p-5">{children}</div>
      </main>
    </div>
  );
}

export const dynamic = "force-dynamic";
