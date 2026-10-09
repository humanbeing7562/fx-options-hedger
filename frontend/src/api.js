// Every call to the FastAPI server goes through here.
// The page is served by the same server (or by Vite's proxy while developing),
// so paths are relative: "/sessions/abc123/next".

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

// Turns the server's error body into one readable line.
// 400: {"detail": "the Session's own message"}
// 422: {"detail": [{"where": [...], "problem": "..."}]}
function errorText(body) {
  if (body == null || body.detail == null) {
    return "request failed";
  }
  if (typeof body.detail === "string") {
    return body.detail;
  }
  const parts = [];
  for (const item of body.detail) {
    const where = (item.where || []).filter((p) => p !== "body").join(".");
    parts.push(where ? `${where}: ${item.problem}` : item.problem);
  }
  return parts.join("; ");
}

async function request(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = null;
  try {
    data = await response.json();
  } catch (error) {
    data = null;
  }
  if (!response.ok) {
    throw new ApiError(response.status, errorText(data));
  }
  return data;
}

function sessionPath(id, rest) {
  return `/sessions/${encodeURIComponent(id)}${rest || ""}`;
}

export const api = {
  fixings: () => request("GET", "/fixings"),

  createSession: (settings) => request("POST", "/sessions", settings),
  deleteSession: (id) => request("DELETE", sessionPath(id)),
  state: (id) => request("GET", sessionPath(id)),

  next: (id) => request("POST", sessionPath(id, "/next")),
  run: (id, until) => request("POST", sessionPath(id, "/run"), { until }),

  quote: (id, order) => request("POST", sessionPath(id, "/quote"), order),
  submitOrder: (id, order) => request("POST", sessionPath(id, "/orders"), order),
  orders: (id) => request("GET", sessionPath(id, "/orders")),
  cancelOrder: (id, orderId) => request("DELETE", sessionPath(id, `/orders/${orderId}`)),
  spot: (id, trade) => request("POST", sessionPath(id, "/spot"), trade),

  recommend: (id) => request("GET", sessionPath(id, "/hedge/recommend")),
  hedge: (id, plan) => request("POST", sessionPath(id, "/hedge"), { plan }),
  hedgeDelta: (id) => request("POST", sessionPath(id, "/hedge/delta")),

  market: (id) => request("GET", sessionPath(id, "/market")),
  history: (id, start) => request("GET", sessionPath(id, `/history?start=${start}`)),
  blotter: (id) => request("GET", sessionPath(id, "/blotter")),
  pnl: (id) => request("GET", sessionPath(id, "/pnl")),
};
