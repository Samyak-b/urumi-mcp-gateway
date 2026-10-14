import { useEffect, useState } from "react";
import { api } from "../api.js";
import { KeyTable, NewKey } from "../keys.jsx";

// Every user: create your own API key and connect an MCP client with just the gateway URL and that key.
export default function Connect({ me }) {
  const [keys, setKeys] = useState([]);
  const [name, setName] = useState("");
  const [created, setCreated] = useState(null);
  const [error, setError] = useState("");

  const load = () => api("/keys").then(setKeys).catch((e) => setError(e.message));
  useEffect(() => { load(); }, []);

  const create = async (e) => {
    e.preventDefault();
    setError("");
    try {
      setCreated(await api("/keys", { method: "POST", body: { name } }));
      setName("");
      load();
    } catch (err) {
      setError(err.message);
    }
  };

  const revoke = async (k) => {
    if (!confirm(`Revoke "${k.name}"? Clients using it stop working at once.`)) return;
    await api(`/keys/${k.id}`, { method: "DELETE" }).catch((e) => setError(e.message));
    load();
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Connect a client</h1>
          <p className="muted">
            One URL and your own key give Claude Code, Cursor or MCP Inspector the tools of every enabled server.
            You never need the servers' own credentials.
          </p>
        </div>
      </div>
      {error && <p className="error">{error}</p>}

      <div className="card">
        <h2>Gateway URL</h2>
        <pre>{me.mcp_url}</pre>
      </div>

      <form className="card" onSubmit={create}>
        <h2>Create an API key</h2>
        <div className="row">
          <label>Name it after where you'll use it
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. laptop – Claude Code" required maxLength={100} />
          </label>
          <button className="primary">Create key</button>
        </div>
      </form>

      {created && <NewKey created={created} mcpUrl={me.mcp_url} onDone={() => setCreated(null)} />}

      <div className="card">
        <h2>Your keys</h2>
        <KeyTable keys={keys} onRevoke={revoke} />
      </div>
    </>
  );
}
