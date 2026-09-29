"use client";

import { useState } from "react";

import { Icon } from "../../components/icons";
import { ThemeToggle } from "../../components/theme-toggle";

/** The only screen reachable without a session.
 *
 * A client component because the sign-in has to set a cookie and then make the
 * browser re-request the page it actually wanted: `router.refresh()` would
 * re-render in place, and every server component in that tree has already
 * resolved its data as signed out. A full navigation is the honest way to say
 * "everything you are looking at is now different".
 */
export default function SignIn() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);

    const response = await fetch("/api/auth/sign-in", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ email, password }),
    }).catch(() => null);

    if (!response) {
      setError("Could not reach the server.");
      setBusy(false);
      return;
    }
    if (response.ok) {
      window.location.href = "/";
      return;
    }
    // The API deliberately does not distinguish "no such account" from "wrong
    // password", so this shows what it said rather than inventing a reason.
    const detail = await response.json().catch(() => null);
    setError(detail?.detail ?? "Could not sign in.");
    setBusy(false);
  }

  return (
    <main className="grid min-h-dvh grid-rows-[auto_1fr] px-4 pb-10 pt-3">
      {/* The theme control belongs here as much as on any other screen, and
          this page is the one a person reaches before they can reach any
          other -- so without it, a wrong theme is a dead end rather than an
          inconvenience. It sits in the layout row rather than the centred
          block so it stays put when the form moves. */}
      <div className="flex justify-end">
        <ThemeToggle />
      </div>

      <div className="mx-auto w-full self-center" style={{ maxWidth: 340 }}>
        <span
          className="mb-4 flex items-center justify-center"
          style={{
            width: 36, height: 36, borderRadius: "var(--radius-md)",
            background: "var(--accent)", color: "var(--accent-ink)",
          }}
          aria-hidden
        >
          <Icon name="spark" size={19} filled />
        </span>
        <h1 className="text-[17px] font-semibold tracking-tight">AI Sales Employee</h1>
        <p className="mt-1 text-[12px]" style={{ color: "var(--muted)" }}>
          Sign in to see your shop&rsquo;s conversations.
        </p>

        <form onSubmit={submit} className="mt-6 flex flex-col gap-3">
          <label className="flex flex-col gap-1.5">
            <span className="text-[11px] font-medium" style={{ color: "var(--muted)" }}>
              Email
            </span>
            <input
              type="email"
              required
              autoComplete="username"
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="inp"
            />
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="text-[11px] font-medium" style={{ color: "var(--muted)" }}>
              Password
            </span>
            <input
              type="password"
              required
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="inp"
            />
          </label>

          {/* Reserved space, so the button does not jump when an error lands
              under the field the user is already reaching for. */}
          <p
            role="alert"
            aria-live="polite"
            className="min-h-[16px] text-[12px]"
            style={{ color: "var(--critical)" }}
          >
            {error ?? ""}
          </p>

          <button
            type="submit"
            disabled={busy}
            className="px-4 py-2 text-[13px] font-medium disabled:opacity-50"
            style={{
              borderRadius: "var(--radius-md)",
              background: "var(--accent)",
              color: "var(--accent-ink)",
            }}
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </main>
  );
}
