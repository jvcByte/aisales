"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { PaneBoundary } from "../../../components/pane-boundary";
import { Status } from "../../../components/ui";
import { type Conversation, type Message, get, post, relativeTime } from "../../../lib/api";

export default function Conversations() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  // `initialized` rather than "is the list empty": an empty result is not the
  // same as never having loaded, and conflating them makes the skeleton
  // reappear on a legitimately quiet day.
  const [initialized, setInitialized] = useState(false);
  const [fetching, setFetching] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [notice, setNotice] = useState("");
  // A first load that fails used to leave the skeleton up forever, with no
  // explanation: the catch swallowed the error and `initialized` never became
  // true, so the pane boundary never saw anything to catch either.
  const [failed, setFailed] = useState<string | null>(null);
  const selectedRef = useRef<string | null>(null);

  const load = useCallback(async () => {
    setFetching(true);
    try {
      const data = await get<{ conversations: Conversation[] }>("/conversations");
      setConversations(data.conversations);
      setInitialized(true);
      setFailed(null);
    } catch (error) {
      // Only worth showing if we have never loaded -- a dropped poll after a
      // good load keeps the last data on screen, as it should.
      setInitialized((done) => {
        if (!done) setFailed(error instanceof Error ? error.message : "could not reach the API");
        return done;
      });
    } finally {
      setFetching(false);
    }
  }, []);

  const loadThread = useCallback(async (id: string) => {
    try {
      const data = await get<{ messages: Message[] }>(`/conversations/${id}/messages`);
      setMessages(data.messages);
    } catch {
      /* as above */
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 5000);
    return () => clearInterval(timer);
  }, [load]);

  useEffect(() => {
    selectedRef.current = selected;
    if (selected) void loadThread(selected);
  }, [selected, loadThread]);

  useEffect(() => {
    const timer = setInterval(() => {
      // Paused while the composer has content, so a poll never clobbers what
      // someone is in the middle of typing.
      if (selectedRef.current && !draft) void loadThread(selectedRef.current);
    }, 3000);
    return () => clearInterval(timer);
  }, [draft, loadThread]);

  // On a phone the panes are exclusive: the list, or one thread. On a wide
  // screen both are visible, so an unpicked list still shows its first thread
  // rather than an empty half-screen.
  const currentId = selected ?? conversations[0]?.id ?? null;
  const thread = conversations.find((c) => c.id === currentId) ?? null;
  const humanOwns = thread?.status === "human";

  async function act(path: string, body?: unknown) {
    if (!selected) return;
    setNotice("");
    try {
      await post(`/conversations/${selected}/${path}`, body ?? {});
      await load();
      await loadThread(selected);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "failed");
    }
  }

  async function send() {
    const text = draft.trim();
    if (!text || !selected) return;
    setDraft("");
    await act("messages", { text });
  }

  if (!initialized) {
    if (failed) {
      return (
        <div className="card p-5">
          <p className="text-[14px] font-medium">Conversations could not load.</p>
          <p className="mt-1 text-[13px]" style={{ color: "var(--muted)" }}>{failed}</p>
          <p className="mt-2 text-[12px]" style={{ color: "var(--faint)" }}>
            Start the API with <span className="num">python -m aisales_api</span>.
          </p>
          <button onClick={() => { setFailed(null); void load(); }}
                  className="btn mt-3">Try again</button>
        </div>
      );
    }
    return (
      <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
        {/* One pane on a phone: two skeletons stacked looks like a page that
            has broken rather than one that is loading. */}
        <div className="card h-[60dvh] animate-pulse lg:h-[70dvh]" />
        <div className="hidden h-[60dvh] animate-pulse lg:block lg:h-[70dvh]" />
      </div>
    );
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
      <PaneBoundary label="Conversations" className={selected ? "hidden lg:block" : ""}>
        <div className="card flex h-[60dvh] flex-col overflow-hidden lg:h-[70dvh]">
          <div className="flex items-center justify-between px-4 py-3 text-[12px]"
               style={{ borderBottom: "1px solid var(--line-soft)", color: "var(--muted)" }}>
            <span className="num">{conversations.length} open</span>
            {fetching && <span>refreshing…</span>}
          </div>
          <ul className="flex-1 overflow-y-auto" data-fetching={fetching ? "1" : "0"}>
            {conversations.length === 0 && (
              <li className="px-4 py-8 text-center text-[13px]" style={{ color: "var(--muted)" }}>
                No conversations yet.
              </li>
            )}
            {conversations.map((c) => (
              <li key={c.id}>
                <button
                  onClick={() => setSelected(c.id)}
                  className="w-full px-4 py-3 text-left"
                  style={{
                    borderBottom: "1px solid var(--line-soft)",
                    background: c.id === selected
                      ? "color-mix(in oklab, var(--accent) 10%, transparent)" : "transparent",
                    // The accent's second job: this one needs a person.
                    boxShadow: c.needs_attention ? "inset 3px 0 var(--warning)" : undefined,
                  }}
                >
                  <div className="flex items-baseline gap-2">
                    <span className="num text-[13px]">{c.phone_e164}</span>
                    <span className="num ml-auto text-[11px]" style={{ color: "var(--faint)" }}>
                      {relativeTime(c.last_message_at)}
                    </span>
                  </div>
                  <div className="mt-0.5 truncate text-[12px]" style={{ color: "var(--muted)" }}>
                    {c.last_body ?? "—"}
                  </div>
                  <div className="mt-1.5 flex gap-1.5">
                    <Status>{c.status === "human" ? "Staff" : "AI"}</Status>
                    {c.needs_attention && <Status tone="warning">Needs you</Status>}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </div>
      </PaneBoundary>

      <PaneBoundary label="This thread" className={selected ? "" : "hidden lg:block"}>
        {!thread ? (
          <div className="card p-8 text-[13px]" style={{ color: "var(--muted)" }}>
            Pick a conversation.
          </div>
        ) : (
          <div className="card flex h-[60dvh] flex-col overflow-hidden lg:h-[70dvh]">
            <div className="flex flex-wrap items-center gap-2 px-4 py-3"
                 style={{ borderBottom: "1px solid var(--line-soft)" }}>
              <span className="num text-[13px] font-medium">{thread.phone_e164}</span>
              <Status tone={humanOwns ? "warning" : "neutral"}>
                {humanOwns ? "you have this" : "AI handling"}
              </Status>
              {thread.attention_reason && (
                <Status tone="warning">{thread.attention_reason}</Status>
              )}
              <div className="ml-auto flex items-center gap-2">
                <button onClick={() => setSelected(null)} className="btn lg:hidden">
                  ← All chats
                </button>
                {humanOwns ? (
                  <button onClick={() => act("handback")}
                          className="chip" style={{ padding: "5px 10px" }}>
                    Hand back to AI
                  </button>
                ) : (
                  <button onClick={() => act("takeover", { as: "owner" })}
                          className="chip" style={{ padding: "5px 10px" }}>
                    Take over
                  </button>
                )}
              </div>
            </div>

            <div className="flex-1 space-y-2 overflow-y-auto p-4">
              {messages.map((m) => (
                <div
                  key={m.id}
                  className={`max-w-[78%] px-3 py-2 ${m.role === "customer" ? "" : "ml-auto"}`}
                  style={{
                    borderRadius: "var(--radius-md)",
                    background: m.role === "customer" ? "var(--panel-2)" : "color-mix(in oklab, var(--accent) 12%, transparent)",
                  }}
                >
                  <div className="mb-0.5 text-[10px] uppercase tracking-wide" style={{ color: "var(--faint)" }}>
                    {m.role}
                    {m.meta?.intent ? ` · ${String(m.meta.intent).replace(/_/g, " ")}` : ""}
                  </div>
                  <div className="whitespace-pre-wrap text-[13px]">{m.body}</div>
                </div>
              ))}
            </div>

            <div className="p-4" style={{ borderTop: "1px solid var(--line-soft)" }}>
              {notice && (
                <p className="mb-2 text-[12px]" style={{ color: "var(--critical)" }}>{notice}</p>
              )}
              <div className="flex gap-2">
                <input
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && void send()}
                  disabled={!humanOwns}
                  placeholder={humanOwns ? "Reply as yourself…" : "The AI is handling this thread"}
                  className="flex-1 px-3 py-2 text-[13px] disabled:opacity-50"
                  style={{
                    borderRadius: "var(--radius-md)",
                    border: "1px solid var(--line)",
                    background: "var(--panel-2)",
                    color: "var(--ink)",
                  }}
                />
                <button
                  onClick={() => void send()}
                  disabled={!humanOwns || !draft.trim()}
                  className="px-4 py-2 text-[13px] font-medium disabled:opacity-40"
                  style={{
                    borderRadius: "var(--radius-md)",
                    background: "var(--accent)",
                    color: "var(--accent-ink)",
                  }}
                >
                  Send
                </button>
              </div>
            </div>
          </div>
        )}
      </PaneBoundary>
    </div>
  );
}
