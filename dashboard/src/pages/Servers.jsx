import { useEffect, useState } from "react";
import { api, when } from "../api.js";

const EMPTY = { name: "", slug: "", url: "", auth_type: "none", auth_header: "", credential: "" };

// Upstream MCP servers with their status and tools. Admins add, edit, disable and remove them.
// The list refreshes every 5 s, so a server whose pod dies turns Unreachable without reloading the page.
export default function Servers({ me }) {
  const isAdmin = me.user.role === "admin";
  const [servers, setServers] = useState(null);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState(null); // server id

  const load = () => api("/servers").then(setServers).catch((e) => setError(e.message));
  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  const act = async (fn) => {
    setError("");
    try {
      await fn();
      await load();
    } catch (e) {
      setError(e.message);
    }
  };

  if (!servers) return <p className="muted">Loading servers…</p>;
  const toolCount = servers.filter((s) => s.status === "connected").reduce((n, s) => n + s.tools.length, 0);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Servers</h1>
          <p className="muted">
            {servers.length} servers, {toolCount} tools available through <code>{me.mcp_url}</code>
          </p>
        </div>
        {isAdmin && !adding && <button className="primary" onClick={() => setAdding(true)}>Add server</button>}
      </div>
      {error && <p className="error">{error}</p>}
      {adding && (
        <ServerForm
          title="Add a server"
          initial={EMPTY}
          isNew
          onCancel={() => setAdding(false)}
          onSave={(body) => act(async () => { await api("/servers", { method: "POST", body }); setAdding(false); })}
        />
      )}
      {servers.map((s) =>
        editing === s.id ? (
          <ServerForm
            key={s.id}
            title={`Edit ${s.name}`}
            initial={{ ...EMPTY, ...s, auth_header: s.auth_header || "", credential: "" }}
            hasCredential={s.has_credential}
            onCancel={() => setEditing(null)}
            onSave={(body) => act(async () => { await api(`/servers/${s.id}`, { method: "PATCH", body }); setEditing(null); })}
          />
        ) : (
          <ServerCard key={s.id} s={s} isAdmin={isAdmin} onEdit={() => setEditing(s.id)} act={act} />
        )
      )}
    </>
  );
}

const LABEL = { connected: "Connected", unreachable: "Unreachable", disabled: "Disabled", unknown: "Checking…" };

function ServerCard({ s, isAdmin, onEdit, act }) {
  const remove = () => {
    if (confirm(`Remove ${s.name}? Its tools disappear for everyone.`)) {
      act(() => api(`/servers/${s.id}`, { method: "DELETE" }));
    }
  };
  return (
    <div className={`card server ${s.status !== "connected" ? "off" : ""}`}>
      <div className="server-head">
        <div>
          <h3>{s.name} <span className="muted mono">{s.slug}__</span></h3>
          {isAdmin && (
            <p className="server-meta muted">
              <code>{s.url}</code> · auth: {s.auth_type}
              {s.auth_type === "header" && ` (${s.auth_header})`}
              {s.has_credential && " · credential stored"}
            </p>
          )}
        </div>
        <span className={`badge ${s.status}`}><span className="dot" />{LABEL[s.status] || s.status}</span>
      </div>
      {s.status === "unreachable" && s.last_error && <p className="error">{s.last_error}</p>}
      <p className="server-meta muted">
        {s.tools.length} tools · checked {when(s.last_checked_at)}
        {s.status === "unreachable" && s.tools.length > 0 && " · last known tools, hidden from clients"}
      </p>
      <div className="tools">
        {s.tools.map((t) => <span key={t.name} className="tool" title={t.description}>{t.name}</span>)}
      </div>
      {isAdmin && (
        <div className="actions" style={{ marginTop: 14 }}>
          <button onClick={onEdit}>Edit</button>
          <button onClick={() => act(() => api(`/servers/${s.id}`, { method: "PATCH", body: { enabled: !s.enabled } }))}>
            {s.enabled ? "Disable" : "Enable"}
          </button>
          {s.enabled && <button onClick={() => act(() => api(`/servers/${s.id}/refresh`, { method: "POST" }))}>Check now</button>}
          <button className="danger" onClick={remove}>Remove</button>
        </div>
      )}
    </div>
  );
}

function ServerForm({ title, initial, isNew, hasCredential, onSave, onCancel }) {
  const [f, setF] = useState(initial);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    const body = { name: f.name, url: f.url, auth_type: f.auth_type, auth_header: f.auth_type === "header" ? f.auth_header : null };
    if (isNew) body.slug = f.slug;
    // Editing: an empty credential field means "keep the stored one". It is never sent back to the browser.
    if (f.credential || isNew) body.credential = f.credential || null;
    await onSave(body);
    setBusy(false);
  };

  return (
    <form className="card" onSubmit={submit}>
      <h2>{title}</h2>
      <div className="row">
        <label>Name<input value={f.name} onChange={set("name")} required /></label>
        <label>Tool prefix
          <input value={f.slug} onChange={set("slug")} disabled={!isNew} required pattern="[a-z][a-z0-9-]{1,22}"
                 title="2-23 characters: lowercase letters, digits and hyphens" placeholder="e.g. wiki2" />
        </label>
      </div>
      <label>MCP URL (Streamable HTTP)<input value={f.url} onChange={set("url")} required placeholder="https://example.com/mcp" /></label>
      <div className="row">
        <label>Authentication
          <select value={f.auth_type} onChange={set("auth_type")}>
            <option value="none">None</option>
            <option value="bearer">Bearer token</option>
            <option value="header">Custom header</option>
          </select>
        </label>
        {f.auth_type === "header" && (
          <label>Header name<input value={f.auth_header} onChange={set("auth_header")} required placeholder="X-API-Key" /></label>
        )}
        {f.auth_type !== "none" && (
          <label>{f.auth_type === "bearer" ? "Token" : "Header value"}
            <input type="password" value={f.credential} onChange={set("credential")} autoComplete="off"
                   placeholder={hasCredential ? "stored; leave empty to keep it" : ""} required={isNew || !hasCredential} />
          </label>
        )}
      </div>
      <p className="muted">The credential stays on the gateway. Team members use the server's tools without ever seeing it.</p>
      <div className="actions">
        <button className="primary" disabled={busy}>{busy ? "Saving and checking…" : "Save"}</button>
        <button type="button" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}
