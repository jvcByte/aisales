import { redirect } from "next/navigation";

import { Shell } from "../../components/shell";
import { type Principal, type Status, peek } from "../../lib/api";

/** The business dashboard's chrome.
 *
 * It lives in a route group rather than the root layout so that `/operator`
 * can have its own. Layouts cannot read the pathname, so "which chrome does
 * this page get" can only be answered by where the page sits in the tree --
 * and an operator who also owns a shop would otherwise see that shop's sidebar
 * wrapped around a console that deliberately sees past it.
 *
 * Route groups do not appear in URLs, so every path here is unchanged.
 */
export default async function BusinessLayout({ children }: { children: React.ReactNode }) {
  const status = await peek<Status>("/status");
  if (status) return <Shell status={status}>{children}</Shell>;

  // No status. There are three reasons and they need different answers, so
  // the second call happens only on this path. Without it an operator who
  // pressed "My dashboard" got a business page that threw on every fetch.
  const who = await peek<{ user: Principal }>("/auth/me");
  if (!who) redirect("/sign-in");
  redirect(who.user.is_operator && !who.user.business ? "/operator" : "/sign-in");
}

export const dynamic = "force-dynamic";
