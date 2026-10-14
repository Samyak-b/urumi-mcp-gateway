import { useEffect, useState } from "react";
import { api } from "./api.js";
import Login from "./pages/Login.jsx";
import Join from "./pages/Join.jsx";
import Servers from "./pages/Servers.jsx";
import Connect from "./pages/Connect.jsx";
import Team from "./pages/Team.jsx";
import Audit from "./pages/Audit.jsx";
import Chat from "./pages/Chat.jsx";

const VIEWS = { servers: Servers, chat: Chat, connect: Connect, team: Team, audit: Audit };

// Pages are chosen by the URL hash (#/servers, #/join/<token>): no router library, and the nginx container only
// ever serves index.html.
const PAGES = [
  { id: "servers", label: "Servers" },
  { id: "chat", label: "Chat" },
  { id: "team", label: "Team", admin: true },
  { id: "connect", label: "Connect a client" },
  { id: "audit", label: "Audit log" },
];

function useHash() {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash.replace(/^#\/?/, "");
}

export default function App() {
  const route = useHash();
  const [me, setMe] = useState(undefined); // undefined = loading, null = signed out

  const loadMe = () => api("/me").then(setMe).catch(() => setMe(null));
  useEffect(() => { loadMe(); }, []);

  if (route.startsWith("join/")) {
    return <Join token={route.slice(5)} onJoined={() => { window.location.hash = "#/connect"; loadMe(); }} />;
  }
  if (me === undefined) return <div className="center muted">Loading…</div>;
  if (me === null) return <Login onLogin={loadMe} />;

  const isAdmin = me.user.role === "admin";
  const pages = PAGES.filter((p) => !p.admin || isAdmin);
  const page = pages.find((p) => p.id === route.split("/")[0]) || pages[0];

  const signOut = async () => {
    await api("/auth/logout", { method: "POST" });
    setMe(null);
  };

  return (
    <div className="shell">
      <header className="top">
        <div className="brand">Team MCP Gateway</div>
        <nav>
          {pages.map((p) => (
            <a key={p.id} href={`#/${p.id}`} className={p.id === page.id ? "active" : ""}>{p.label}</a>
          ))}
        </nav>
        <div className="who">
          <span>{me.user.email}</span>
          <span className={`role ${me.user.role}`}>{me.user.role}</span>
          <button className="link" onClick={signOut}>Sign out</button>
        </div>
      </header>
      <main>
        <Page id={page.id} me={me} />
      </main>
    </div>
  );
}

function Page({ id, me }) {
  const View = VIEWS[id];
  return <View me={me} />;
}
