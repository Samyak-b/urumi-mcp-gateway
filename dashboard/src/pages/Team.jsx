import { useEffect, useState } from "react";
import { api, when } from "../api.js";
import { KeyTable, NewKey } from "../keys.jsx";

// Admin only: invite members, change roles, disable users, issue and revoke anyone's API keys.
export default function Team({ me }) {
  const [users, setUsers] = useState([]);
  const [invites, setInvites] = useState([]);
  const [keys, setKeys] = useState([]);
  const [error, setError] = useState("");
  const [inviteLink, setInviteLink] = useState(null);
  const [issued, setIssued] = useState(null); // a key issued for someone, shown once
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");

  const load = async () => {
    try {
      const [u, i, k] = await Promise.all([api("/users"), api("/invites"), api("/keys?all=true")]);
      setUsers(u);
      setInvites(i);
      setKeys(k);
    } catch (e) {
      setError(e.message);
    }
  };
  useEffect(() => { load(); }, []);

  const act = async (fn) => {
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e.message);
    }
    await load();
  };

  const invite = (e) => {
    e.preventDefault();
    act(async () => {
      const res = await api("/invites", { method: "POST", body: { email, role } });
      setInviteLink(res);
      setEmail("");
    });
  };

  const issueKey = (u) => act(async () => {
    setIssued(await api("/keys", { method: "POST", body: { name: `issued by ${me.user.email}`, user_id: u.id } }));
  });

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Team</h1>
          <p className="muted">Admins manage servers and members. Members use the tools and manage their own keys.</p>
        </div>
      </div>
      {error && <p className="error">{error}</p>}

      <div className="grid2">
        <form className="card" onSubmit={invite}>
          <h2>Invite a teammate</h2>
          <div className="row">
            <label>Email<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required /></label>
            <label style={{ flex: "0 0 130px" }}>Role
              <select value={role} onChange={(e) => setRole(e.target.value)}>
                <option value="member">Member</option>
                <option value="admin">Admin</option>
              </select>
            </label>
          </div>
          <button className="primary">Create invite link</button>
          {inviteLink && (
            <div className="secret">
              Send this link to <b>{inviteLink.email}</b>. It works once, for 7 days:
              <pre>{inviteLink.invite_url}</pre>
              <button type="button" onClick={() => navigator.clipboard.writeText(inviteLink.invite_url)}>Copy link</button>
            </div>
          )}
        </form>
        <div className="card">
          <h2>Pending invites</h2>
          {invites.length === 0 && <p className="muted">None.</p>}
          <table><tbody>
            {invites.map((i) => (
              <tr key={i.id}>
                <td>{i.email}</td><td><span className={`role ${i.role}`}>{i.role}</span></td>
                <td className="muted">expires {when(i.expires_at)}</td>
                <td><button className="link" onClick={() => act(() => api(`/invites/${i.id}`, { method: "DELETE" }))}>Cancel</button></td>
              </tr>
            ))}
          </tbody></table>
        </div>
      </div>

      {issued && <NewKey created={issued} onDone={() => setIssued(null)} note={`This key belongs to ${issued.owner_email}. Pass it on safely; it is not shown again.`} />}

      <div className="card">
        <h2>Members</h2>
        <table>
          <thead><tr><th>User</th><th>Role</th><th>Active keys</th><th>Status</th><th /></tr></thead>
          <tbody>
            {users.map((u) => {
              const self = u.id === me.user.id;
              return (
                <tr key={u.id}>
                  <td>{u.email}<div className="muted">{u.name}</div></td>
                  <td>
                    <select value={u.role} disabled={self} style={{ width: 120 }}
                            onChange={(e) => act(() => api(`/users/${u.id}`, { method: "PATCH", body: { role: e.target.value } }))}>
                      <option value="member">Member</option>
                      <option value="admin">Admin</option>
                    </select>
                  </td>
                  <td>{u.active_keys}</td>
                  <td>{u.disabled ? <span className="badge disabled">Disabled</span> : <span className="badge ok">Active</span>}</td>
                  <td className="actions">
                    {!u.disabled && <button onClick={() => issueKey(u)}>Issue key</button>}
                    {!self && (
                      <button className={u.disabled ? "" : "danger"}
                              onClick={() => act(() => api(`/users/${u.id}`, { method: "PATCH", body: { disabled: !u.disabled } }))}>
                        {u.disabled ? "Enable" : "Disable"}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h2>All API keys</h2>
        <KeyTable keys={keys} showOwner onRevoke={(k) => act(() => api(`/keys/${k.id}`, { method: "DELETE" }))} />
      </div>
    </>
  );
}
