"use client";

import { Component, type ReactNode } from "react";

// A boundary that wraps a *region*, not a page.
//
// A class component because React has no hook for this, and a failed thread
// must not take the conversation list with it: the two panes sit side by side
// precisely so one can break alone.
export class PaneBoundary extends Component<
  { label: string; children: ReactNode; className?: string },
  { failed: string | null }
> {
  state = { failed: null as string | null };

  static getDerivedStateFromError(error: Error) {
    return { failed: error.message };
  }

  componentDidCatch(error: Error) {
    // Reported where it was caught. Where the fallback renders is a separate
    // decision from where the error is logged.
    console.error(`[${this.props.label}]`, error);
  }

  render() {
    if (this.state.failed) {
      return (
        <div className={`card p-4 ${this.props.className ?? ""}`}>
          <p className="text-[13px] font-medium">{this.props.label} failed to load</p>
          <p className="mt-1 text-[12px]" style={{ color: "var(--muted)" }}>
            {this.state.failed}
          </p>
          <button
            onClick={() => this.setState({ failed: null })}
            className="mt-3 rounded border px-2 py-1 text-[12px]"
            style={{ borderColor: "var(--line)" }}
          >
            Retry
          </button>
        </div>
      );
    }
    // `min-w-0` because a pane is a grid item, and a grid item's default
    // minimum is its content -- which is how one wide message pushes the
    // whole layout past the viewport.
    const cls = ["min-w-0", this.props.className].filter(Boolean).join(" ");
    return <div className={cls}>{this.props.children}</div>;
  }
}
