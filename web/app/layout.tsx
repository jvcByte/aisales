import type { Metadata, Viewport } from "next";

import { THEME_SCRIPT } from "../components/theme-script";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Sales Employee",
  description: "Conversations, leads and orders for your shop",
};

// Without this the page renders at desktop width on a phone and the browser
// scales it down, so every tap target is a fifth of the size it looks.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // The two colours the browser paints its own chrome with, so the URL bar
  // matches the app rather than flashing white over a dark theme.
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f4f5f3" },
    { media: "(prefers-color-scheme: dark)", color: "#0b0e13" },
  ],
};

/** The document, and nothing else.
 *
 * What wraps the content is decided one level down, by route group: the
 * business dashboard draws the shell, the operator console draws its own,
 * and the sign-in page draws neither. Putting the shell here would mean every
 * page got it, including the two that must not.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // `suppressHydrationWarning` because THEME_SCRIPT sets `data-theme` and
    // `color-scheme` on this element before React hydrates. That difference is
    // the entire point of the script -- it is what stops the page flashing the
    // wrong theme -- and React would otherwise report every visit to every
    // page as a hydration mismatch, which is the kind of permanent noise that
    // hides the next real one.
    <html lang="en-NG" suppressHydrationWarning>
      <head>
        {/* A plain inline script, in <head>, and it has to be all three.
            `next/script` with `beforeInteractive` looks like the supported way
            to do this and is not: Next emits it as a queued `self.__next_s`
            entry that its own bootstrap drains after the first chunks load, so
            it runs *after* the page has begun painting -- which is exactly the
            flash this exists to prevent. Verified by reading the served HTML,
            not by reading the docs. */}
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
