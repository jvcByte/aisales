"use client";

import { useEffect, useState } from "react";

import { THEME_KEY as KEY } from "./theme-script";

type Choice = "system" | "light" | "dark";



export function ThemeToggle() {
  const [choice, setChoice] = useState<Choice>("system");
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let stored: Choice = "system";
    try {
      const value = localStorage.getItem(KEY);
      if (value === "light" || value === "dark") stored = value;
    } catch {
      /* storage blocked; the OS preference is a fine answer */
    }
    setChoice(stored);
    setReady(true);
  }, []);

  function apply(next: Choice) {
    setChoice(next);
    const root = document.documentElement;
    const theme =
      next === "system"
        ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
        : next;
    root.setAttribute("data-theme", theme);
    root.style.colorScheme = theme;
    try {
      if (next === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, next);
    } catch {
      /* the choice still applies for this page view */
    }
  }

  const options: { key: Choice; label: string; glyph: string }[] = [
    { key: "light", label: "Light", glyph: "☀" },
    { key: "system", label: "System", glyph: "◐" },
    { key: "dark", label: "Dark", glyph: "☾" },
  ];

  return (
    <div
      className="flex items-center gap-0.5 p-0.5"
      style={{ borderRadius: "var(--radius-md)", border: "1px solid var(--line)" }}
      role="group"
      aria-label="Colour theme"
    >
      {options.map((option) => (
        <button
          key={option.key}
          onClick={() => apply(option.key)}
          aria-pressed={choice === option.key}
          title={`${option.label} theme`}
          className="px-2 py-1 text-[12px]"
          style={{
            borderRadius: "var(--radius-sm)",
            background: choice === option.key
              ? "color-mix(in oklab, var(--accent) 16%, transparent)" : "transparent",
            color: choice === option.key ? "var(--ink)" : "var(--muted)",
            // Faded until the stored choice is read, so the control never shows
            // the wrong option highlighted for a frame.
            opacity: ready ? 1 : 0.4,
          }}
        >
          <span aria-hidden>{option.glyph}</span>
          <span className="sr-only">{option.label}</span>
        </button>
      ))}
    </div>
  );
}
