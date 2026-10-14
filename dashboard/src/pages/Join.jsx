import { useEffect, useState } from "react";
import { api } from "../api.js";

// The page an invite link opens: #/join/<token>.
export default function Join({ token, onJoined }) {
  const [invite, setInvite] = useState(null);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");

  useEffect(() => {
    api(`/join/${token}`).then(setInvite).catch((err) => setError(err.message));
  }, [token]);

  const submit = async (e) => {
    e.preventDefault();
    setError("");
    try {
      await api("/join", { method: "POST", body: { token, name, password } });
      onJoined();
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <div className="center">
      <form className="card narrow" onSubmit={submit}>
        <h1>Join the team</h1>
        {!invite && !error && <p className="muted">Checking the invite…</p>}
        {invite && (
          <>
            <p className="muted">You were invited as <b>{invite.email}</b> ({invite.role}).</p>
            <label>Your name<input value={name} onChange={(e) => setName(e.target.value)} autoFocus required /></label>
            <label>Choose a password (10+ characters)
              <input type="password" minLength={10} value={password} onChange={(e) => setPassword(e.target.value)} required />
            </label>
            <button className="primary">Create my account</button>
          </>
        )}
        {error && <p className="error">{error}</p>}
      </form>
    </div>
  );
}
