"""The simulator page: one HTML string, no build step.

Served by the API rather than the dashboard so the whole product demo is
`python -m aisales_api` plus a browser -- no pnpm install, no Node. The
dashboard is for the business owner; this is for whoever tunes the agent, and
it belongs next to the API it pokes.
"""

# Deliberately a .replace() template rather than str.format(): the CSS and JS
# below are full of braces, and escaping every one of them is a bug waiting to
# happen.
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Simulator &middot; __BUSINESS__</title>
<style>
  :root {
    --bg: #fbfbfa;
    --panel: #ffffff;
    --line: #e4e4e1;
    --ink: #1a1a19;
    --muted: #6b6b66;
    --accent: #1f7a4d;
    --accent-soft: #eaf4ee;
    --customer: #f2f2ef;
    --staff: #eef1f6;
    --radius-sm: 4px;
    --radius-md: 6px;
    --radius-lg: 10px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #16171a; --panel: #1d1f23; --line: #2e3138; --ink: #e8e8e6;
      --muted: #97978f; --accent: #5fbf8c; --accent-soft: #1e3129;
      --customer: #26282d; --staff: #232a33;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 720px; margin: 0 auto; padding: 0 16px 16px; }
  header {
    display: flex; align-items: baseline; gap: 10px;
    padding: 18px 0 12px; border-bottom: 1px solid var(--line);
  }
  header h1 { font-size: 16px; font-weight: 600; margin: 0; letter-spacing: -0.01em; }
  header .tag {
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--muted); border: 1px solid var(--line);
    padding: 2px 6px; border-radius: var(--radius-sm);
  }
  header .slug { margin-left: auto; color: var(--muted); font-size: 12px; }

  #thread { padding: 16px 0; min-height: 320px; display: flex; flex-direction: column; gap: 8px; }
  #thread[data-fetching="1"] { opacity: 0.55; }
  .msg { max-width: 78%; padding: 8px 11px; border-radius: var(--radius-lg);
          white-space: pre-wrap; overflow-wrap: anywhere; }
  .msg .who { display: block; font-size: 10px; letter-spacing: 0.05em;
              text-transform: uppercase; color: var(--muted); margin-bottom: 3px; }
  .msg.customer { align-self: flex-start; background: var(--customer);
                  border-bottom-left-radius: var(--radius-sm); }
  .msg.ai { align-self: flex-end; background: var(--accent-soft);
            border-bottom-right-radius: var(--radius-sm); }
  .msg.staff { align-self: flex-end; background: var(--staff);
               border-bottom-right-radius: var(--radius-sm); }
  .msg.system { align-self: center; background: transparent; color: var(--muted);
                font-size: 12px; max-width: 100%; text-align: center; }
  .empty { color: var(--muted); font-size: 13px; padding: 40px 0; text-align: center; }

  form { display: flex; gap: 8px; padding-top: 12px; border-top: 1px solid var(--line); }
  input[type=text] {
    flex: 1; padding: 9px 11px; font: inherit; color: var(--ink);
    background: var(--panel); border: 1px solid var(--line);
    border-radius: var(--radius-md);
  }
  input[type=text]:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  button {
    font: inherit; padding: 9px 14px; cursor: pointer; color: var(--ink);
    background: var(--panel); border: 1px solid var(--line);
    border-radius: var(--radius-md);
  }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button:disabled { opacity: 0.5; cursor: default; }
  .tools { display: flex; gap: 6px; flex-wrap: wrap; padding-top: 10px; }
  .tools button { font-size: 12px; padding: 5px 9px; color: var(--muted); }
  .status { font-size: 12px; color: var(--muted); padding-top: 8px; min-height: 18px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>__BUSINESS__</h1>
    <span class="tag">simulator</span>
    <span class="slug">__SLUG__</span>
  </header>

  <div id="thread"><div class="empty">No messages yet. Say something a customer would.</div></div>

  <form id="composer">
    <input id="text" type="text" autocomplete="off" autofocus
           placeholder="Abeg how much for the lace?">
    <button class="primary" type="submit">Send</button>
  </form>

  <div class="tools">
    <button type="button" data-act="burst">Send 3 in a row</button>
    <button type="button" data-act="resend">Resend last (dedupe check)</button>
    <button type="button" data-act="clear">Clear view</button>
  </div>
  <div class="status" id="status"></div>
</div>

<script>
const SLUG = "__SLUG__";
const thread = document.getElementById("thread");
const statusEl = document.getElementById("status");
const input = document.getElementById("text");
const sendBtn = document.querySelector("button.primary");
let conversationId = null;
let lastPayload = null;

function setStatus(text) { statusEl.textContent = text || ""; }

function render(messages) {
  if (!messages.length) {
    thread.innerHTML = '<div class="empty">No messages yet. Say something a customer would.</div>';
    return;
  }
  thread.textContent = "";
  for (const m of messages) {
    const el = document.createElement("div");
    el.className = "msg " + m.role;
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = m.role === "customer" ? "customer"
                    : m.role === "ai" ? "ai" : m.role;
    const body = document.createElement("span");
    body.textContent = m.body;            // textContent, never innerHTML
    el.append(who, body);
    thread.append(el);
  }
  thread.scrollTop = thread.scrollHeight;
}

async function send(payload, label) {
  sendBtn.disabled = true;
  setStatus(label || "sending\\u2026");
  try {
    const res = await fetch("/sim/messages", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ slug: SLUG, ...payload }),
    });
    if (!res.ok) { setStatus("failed: " + res.status + " " + await res.text()); return; }
    const data = await res.json();
    conversationId = data.conversation_id;
    lastPayload = payload;
    render(data.messages);
    setStatus(data.duplicate ? "duplicate ignored (nothing recorded)"
            : data.enqueued ? "recorded, turn queued"
            : "recorded, turn already pending \\u2014 will be folded in");
  } catch (err) {
    setStatus("failed: " + err);
  } finally {
    sendBtn.disabled = false;
  }
}

document.getElementById("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  send({ text, from: "08031234567" });
});

document.querySelector(".tools").addEventListener("click", async (e) => {
  const act = e.target.dataset.act;
  if (!act) return;
  if (act === "burst") {
    // Three messages in quick succession. The agent must answer once, having
    // seen all three -- that is jobs_one_turn_idx doing its job.
    await send({ text: "Hello", from: "08031234567" }, "burst 1/3\\u2026");
    await send({ text: "You dey?", from: "08031234567" }, "burst 2/3\\u2026");
    await send({ text: "Abeg how much for the lace?", from: "08031234567" }, "burst 3/3\\u2026");
  } else if (act === "resend") {
    if (!lastPayload) { setStatus("nothing sent yet"); return; }
    send(lastPayload, "resending the same provider message id\\u2026");
  } else if (act === "clear") {
    thread.innerHTML = '<div class="empty">Cleared. Reload to fetch the thread again.</div>';
    setStatus("");
  }
});

// Poll, keeping the current thread on screen while it runs. A blank flash
// between refreshes reads as the tool being broken.
async function poll() {
  if (!conversationId) return;
  thread.dataset.fetching = "1";
  try {
    const res = await fetch("/conversations/" + conversationId + "/messages");
    if (res.ok) render((await res.json()).messages);
  } catch (_) { /* a dropped poll is not worth reporting */ }
  finally { thread.dataset.fetching = "0"; }
}
setInterval(poll, 3000);
</script>
</body>
</html>
"""
