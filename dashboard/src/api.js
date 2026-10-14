// Every call goes to the same origin (/api), so the session cookie travels along on its own.
// X-Requested-With is the gateway's CSRF check: another site can't make the browser send it.
export async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-With": "dashboard" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data?.detail;
    // FastAPI validation errors arrive as a list of {loc, msg}.
    const message = Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : detail || `HTTP ${res.status}`;
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  return data;
}

export function when(ts) {
  return ts ? new Date(ts).toLocaleString() : "never";
}
