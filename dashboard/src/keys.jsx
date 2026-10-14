import { when } from "./api.js";

// Shown on the Connect page (your own keys) and the Team page (everyone's keys).

export function KeyTable({ keys, showOwner, onRevoke }) {
  if (keys.length === 0) return <p className="muted">No keys yet.</p>;
  return (
    <table>
      <thead>
        <tr>{showOwner && <th>Owner</th>}<th>Name</th><th>Key</th><th>Created</th><th>Last used</th><th>Status</th><th /></tr>
      </thead>
      <tbody>
        {keys.map((k) => (
          <tr key={k.id}>
            {showOwner && <td>{k.owner_email}</td>}
            <td>{k.name}</td>
            <td><code>{k.prefix}…</code></td>
            <td className="muted">{when(k.created_at)}</td>
            <td className="muted">{when(k.last_used_at)}</td>
            <td>{k.revoked_at ? <span className="badge rejected">Revoked</span> : <span className="badge ok">Active</span>}</td>
            <td>{!k.revoked_at && <button className="danger" onClick={() => onRevoke(k)}>Revoke</button>}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// A freshly created key with ready-to-paste client settings. The gateway stores only a hash, so this is the
// only time anyone can see the key.
export function NewKey({ created, mcpUrl, onDone, note }) {
  const url = mcpUrl || `${window.location.origin}/mcp`;
  const claude = `claude mcp add --transport http team-gateway ${url} --header "Authorization: Bearer ${created.key}"`;
  const cursor = JSON.stringify(
    { mcpServers: { "team-gateway": { url, headers: { Authorization: `Bearer ${created.key}` } } } }, null, 2);
  return (
    <div className="card secret">
      <h2>Copy your key now</h2>
      <p>{note || "It is shown only this once. If you lose it, revoke it and create a new one."}</p>
      <pre>{created.key}</pre>
      <h3 style={{ marginTop: 12 }}>Claude Code</h3>
      <pre>{claude}</pre>
      <h3 style={{ marginTop: 12 }}>Cursor (.cursor/mcp.json)</h3>
      <pre>{cursor}</pre>
      <h3 style={{ marginTop: 12 }}>MCP Inspector</h3>
      <p className="muted">
        Run <code>npx @modelcontextprotocol/inspector</code>, choose Streamable HTTP, enter the URL above and add the
        header <code>Authorization: Bearer &lt;key&gt;</code>.
      </p>
      <div className="actions">
        <button onClick={() => navigator.clipboard.writeText(created.key)}>Copy key</button>
        <button onClick={() => navigator.clipboard.writeText(claude)}>Copy Claude Code command</button>
        <button className="primary" onClick={onDone}>I saved it</button>
      </div>
    </div>
  );
}
