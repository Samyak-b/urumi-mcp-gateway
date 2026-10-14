import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";

const STARTERS = [
  "What orders came in today?",
  "Create a product called Linen Apron for 24.00",
  "Summarise today's orders and save it as a team note",
];

// The chat agent. It answers with the same tools an MCP client gets, as the signed-in user: every tool it calls
// is in the audit log under your name with source "Chat". The tools it called are shown above each answer.
export default function Chat({ me }) {
  const [messages, setMessages] = useState([]); // {role, content, steps?}
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const end = useRef(null);

  // Braces matter: newer browsers return a Promise from scrollIntoView, and React would take a returned value
  // for the effect's cleanup function and crash.
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, busy]);

  const send = async (text) => {
    const content = (text ?? draft).trim();
    if (!content || busy) return;
    const next = [...messages, { role: "user", content }];
    setMessages(next);
    setDraft("");
    setBusy(true);
    setError("");
    try {
      // Only the text turns go back to the server; tools run again for the new question.
      const res = await api("/chat", { method: "POST", body: { messages: next.map(({ role, content }) => ({ role, content })) } });
      setMessages([...next, { role: "assistant", content: res.reply, steps: res.steps }]);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Chat</h1>
          <p className="muted">Answers come from the team's tools, called as you ({me.user.email}).</p>
        </div>
        {messages.length > 0 && <button onClick={() => { setMessages([]); setError(""); }}>New conversation</button>}
      </div>
      {!me.chat_configured && <p className="notice">The chat agent has no LLM configured yet (LLM_BASE_URL, LLM_MODEL and the LLM_API_KEY secret).</p>}

      <div className="card chat">
        {messages.length === 0 && (
          <div className="starters">
            <p className="muted">Try one of these:</p>
            {STARTERS.map((s) => <button key={s} onClick={() => send(s)} disabled={busy}>{s}</button>)}
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            {m.steps?.length > 0 && (
              <div className="trace">
                {m.steps.map((s, j) => (
                  <details key={j}>
                    <summary>
                      <span className={`badge ${s.ok ? "ok" : "error"}`}>{s.ok ? "ok" : "error"}</span>
                      <code>{s.tool}</code> <span className="muted">{s.duration_ms} ms</span>
                    </summary>
                    <pre>{JSON.stringify(s.arguments, null, 2)}</pre>
                    <pre>{s.result_preview}</pre>
                  </details>
                ))}
              </div>
            )}
            <div className="bubble">{m.content}</div>
          </div>
        ))}
        {busy && <div className="msg assistant"><div className="bubble muted">Thinking and calling tools…</div></div>}
        {error && <p className="error">{error}</p>}
        <div ref={end} />
      </div>

      <form className="composer" onSubmit={(e) => { e.preventDefault(); send(); }}>
        <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="Ask about orders, products or notes…" disabled={busy} />
        <button className="primary" disabled={busy || !draft.trim()}>Send</button>
      </form>
    </>
  );
}
