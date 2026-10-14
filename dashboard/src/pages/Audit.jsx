import { useEffect, useState } from "react";
import { api } from "../api.js";

const OUTCOMES = ["ok", "error", "timeout", "unreachable", "denied", "rejected"];

// Every tool call: who, which server and tool, when, how long, and how it ended.
// Admins see everyone and can filter by user; members see only their own calls.
export default function Audit({ me }) {
  const isAdmin = me.user.role === "admin";
  const [rows, setRows] = useState([]);
  const [users, setUsers] = useState([]);
  const [servers, setServers] = useState([]);
  const [filter, setFilter] = useState({ user_id: "", server: "", outcome: "", via: "" });
  const [live, setLive] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    api("/servers").then(setServers).catch(() => {});
    if (isAdmin) api("/users").then(setUsers).catch(() => {});
  }, [isAdmin]);

  const load = () => {
    const q = new URLSearchParams({ limit: "200" });
    Object.entries(filter).forEach(([k, v]) => v && q.set(k, v));
    api(`/audit?${q}`).then(setRows).catch((e) => setError(e.message));
  };
  useEffect(() => {
    load();
    if (!live) return undefined;
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [filter, live]);

  const set = (k) => (e) => setFilter({ ...filter, [k]: e.target.value });

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Audit log</h1>
          <p className="muted">{isAdmin ? "Every tool call by every user" : "Your tool calls"}, newest first.</p>
        </div>
        <label style={{ margin: 0, display: "flex", gap: 6, alignItems: "center", fontWeight: 400 }}>
          <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} style={{ width: "auto", margin: 0 }} />
          Refresh every 5 s
        </label>
      </div>
      {error && <p className="error">{error}</p>}
      <div className="card">
        <div className="row">
          {isAdmin && (
            <label>User
              <select value={filter.user_id} onChange={set("user_id")}>
                <option value="">Everyone</option>
                {users.map((u) => <option key={u.id} value={u.id}>{u.email}</option>)}
              </select>
            </label>
          )}
          <label>Server
            <select value={filter.server} onChange={set("server")}>
              <option value="">All</option>
              {servers.map((s) => <option key={s.slug} value={s.slug}>{s.slug}</option>)}
            </select>
          </label>
          <label>Outcome
            <select value={filter.outcome} onChange={set("outcome")}>
              <option value="">All</option>
              {OUTCOMES.map((o) => <option key={o}>{o}</option>)}
            </select>
          </label>
          <label>Source
            <select value={filter.via} onChange={set("via")}>
              <option value="">All</option>
              <option value="mcp">MCP client</option>
              <option value="chat">Chat</option>
            </select>
          </label>
        </div>
      </div>
      <div className="card">
        {rows.length === 0 && <p className="muted">No tool calls match.</p>}
        {rows.length > 0 && (
          <table>
            <thead>
              <tr><th>When</th><th>User</th><th>Source</th><th>Server</th><th>Tool</th><th>Time</th><th>Outcome</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td className="muted" style={{ whiteSpace: "nowrap" }}>{new Date(r.ts).toLocaleString()}</td>
                  <td>{r.user_email || "—"}</td>
                  <td>{r.via === "chat" ? "Chat" : "MCP client"}</td>
                  <td><code>{r.server || "—"}</code></td>
                  <td><code>{r.tool || "—"}</code></td>
                  <td style={{ whiteSpace: "nowrap" }}>{r.duration_ms} ms</td>
                  <td>
                    <span className={`badge ${r.outcome}`}>{r.outcome}</span>
                    {r.detail && <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>{r.detail}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
