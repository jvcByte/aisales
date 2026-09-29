"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Icon } from "./icons";

const NAV = [
  { href: "/", label: "Dashboard", icon: "dashboard" },
  { href: "/conversations", label: "Conversations", icon: "conversations", badge: true },
  { href: "/leads", label: "Leads", icon: "leads" },
  { href: "/orders", label: "Orders", icon: "orders" },
  { href: "/catalogue", label: "Catalogue", icon: "catalogue" },
  { href: "/customers", label: "Customers", icon: "customers" },
  { href: "/follow-ups", label: "Follow-ups", icon: "followups" },
  { href: "/payments", label: "Payments", icon: "payments" },
  { href: "/reports", label: "Reports", icon: "reports" },
  { href: "/settings", label: "Settings", icon: "settings" },
];

/** The navigation rail.
 *
 * Fixed from `lg` up, where it sits inside the flex row and never moves. Below
 * that it is an off-canvas drawer: a 224px rail on a 360px screen leaves
 * 136px for the work, which is not a layout, it is a sliver.
 */
export function Sidebar({
  attention,
  channel,
  offline,
  models,
  open,
  onClose,
}: {
  attention: number;
  channel: string;
  offline: boolean;
  models: string[];
  open: boolean;
  onClose: () => void;
}) {
  const pathname = usePathname();

  return (
    <aside
      aria-label="Navigation"
      className={`fixed inset-y-0 left-0 z-40 flex w-[268px] flex-none flex-col overflow-y-auto p-3
                  transition-transform duration-200 ease-out
                  lg:static lg:z-auto lg:w-[224px] lg:translate-x-0 lg:transition-none
                  ${open ? "translate-x-0" : "-translate-x-full"}`}
      style={{ background: "var(--panel-2)", borderRight: "1px solid var(--line)" }}
    >
      <div className="mb-2 flex items-center justify-between lg:hidden">
        <span className="px-2 text-[12px] font-semibold">Menu</span>
        <button onClick={onClose} className="btn" aria-label="Close navigation">✕</button>
      </div>

      <nav className="flex flex-col gap-0.5">
        {NAV.map((item) => {
          const active =
            item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              className="flex items-center gap-2.5 px-2.5 py-2.5 text-[13px] lg:py-2"
              style={{
                borderRadius: "var(--radius-md)",
                background: active
                  ? "color-mix(in oklab, var(--accent) 15%, transparent)" : "transparent",
                color: active ? "var(--ink)" : "var(--muted)",
                fontWeight: active ? 500 : 400,
              }}
            >
              <span style={{ color: active ? "var(--accent)" : "inherit", display: "flex" }}>
                <Icon name={item.icon} size={16} />
              </span>
              {item.label}
              {item.badge && attention > 0 && (
                <span
                  className="num ml-auto px-1.5 text-[10px] font-semibold"
                  style={{ borderRadius: 999, background: "var(--warning)", color: "#231a00" }}
                >
                  {attention}
                </span>
              )}
            </Link>
          );
        })}
      </nav>

      <div className="mt-4 flex flex-col gap-2 pt-2 lg:mt-auto lg:pt-4">
        <div className="card px-3 py-2.5">
          <div className="flex items-center gap-2 text-[12px] font-medium">
            <span style={{ color: "var(--good)", display: "flex" }}>
              <Icon name="conversations" size={14} filled />
            </span>
            WhatsApp Channel
          </div>
          <div className="mt-1 text-[11px]" style={{ color: "var(--muted)" }}>
            {channel === "whatsapp" && !offline ? "Live" : "Simulator (Offline)"}
          </div>
          {offline && (
            <div className="mt-1 text-[10px]" style={{ color: "var(--faint)" }}>
              Connect real WhatsApp to go live.
            </div>
          )}
        </div>

        <div className="card px-3 py-2.5">
          <div className="flex items-center gap-2 text-[12px] font-medium">
            <span style={{ color: "var(--warning)", display: "flex" }}>
              <Icon name="spark" size={14} filled />
            </span>
            AI Model
          </div>
          <div className="mt-1 text-[11px]" style={{ color: "var(--muted)" }}>
            {models.length
              ? `${models[0][0].toUpperCase()}${models[0].slice(1)}${
                  models.length > 1 ? " (fallback chain)" : ""}`
              : "none configured"}
          </div>
          <div className="mt-1.5">
            <span
              className="text-[10px]"
              style={{
                borderRadius: 999,
                padding: "1px 7px",
                background: models.length
                  ? "color-mix(in oklab, var(--good) 22%, transparent)"
                  : "color-mix(in oklab, var(--warning) 22%, transparent)",
                color: models.length ? "var(--good)" : "var(--warning)",
              }}
            >
              {models.length ? "Active" : "No key"}
            </span>
          </div>
        </div>

        <div className="px-2 pb-1 text-[10px]" style={{ color: "var(--faint)" }}>
          AISALES · v0.1.0 {offline ? "· Offline mode" : ""}
        </div>
      </div>
    </aside>
  );
}
