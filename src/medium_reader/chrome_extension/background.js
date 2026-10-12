// Content scripts never receive the local connection token after pairing.
// Fetch destinations and message senders are validated here, not supplied by a page.
async function handle(message, sender) {
  if (!sender.tab || sender.frameId !== 0) throw new Error("Expected the connected browser tab.");
  const url = new URL(sender.url);
  const key = `tab:${sender.tab.id}`;
  if (message.kind === "pair") {
    if (url.hostname !== "127.0.0.1" || url.protocol !== "http:" || url.pathname !== "/connect") {
      throw new Error("Pair from the local reader connection page.");
    }
    if (message.endpoint !== url.origin || !/^[A-Za-z0-9_-]{43}$/.test(message.token)) {
      throw new Error("Invalid connection link. Run /login again.");
    }
    const session = {endpoint: url.origin, token: message.token};
    const paired = await request(session, "/pair", {});
    if (typeof paired.session !== "string") throw new Error("Update the reader and reload this extension.");
    session.session = paired.session;
    // Reconnecting binds this reader to one tab/account; previous tabs stop sharing.
    const previous = await chrome.storage.session.get(null);
    const obsolete = Object.keys(previous).filter(key => previous[key]?.endpoint === url.origin);
    if (obsolete.length) await chrome.storage.session.remove(obsolete);
    await chrome.storage.session.set({[key]: session});
    await chrome.tabs.update(sender.tab.id, {url: "https://medium.com/"});
    return {ok: true};
  }
  if (url.origin !== "https://medium.com") throw new Error("Expected a Medium tab.");
  const session = (await chrome.storage.session.get(key))[key];
  if (!session) return {unpaired: true};
  if (message.kind === "command") return request(session, "/command");
  if (message.kind === "snapshot") {
    const data = message.data;
    if (!data || data.url !== sender.url || typeof data.html !== "string" || data.html.length > 1500000) {
      throw new Error("Invalid Medium snapshot.");
    }
    return request(session, "/snapshot", data);
  }
  throw new Error("Unknown connection message.");
}

async function request(session, path, data) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
  const response = await fetch(session.endpoint + path, {
    method: data ? "POST" : "GET",
    headers: {Authorization: `Bearer ${session.token}`, "Content-Type": "application/json",
              "X-Medium-Reader-Session": session.session || ""},
    body: data ? JSON.stringify(data) : undefined,
    credentials: "omit",
    cache: "no-store",
    signal: controller.signal
  });
  if (!response.ok) {
    const error = new Error("Reader connection closed. Run /login to reconnect.");
    error.retryable = response.status >= 500;
    throw error;
  }
  return await response.json();
  } catch (error) {
    if (typeof error.retryable !== "boolean") error.retryable = true;
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  handle(message, sender).then(respond, error => respond({error: error.message, retryable: !!error.retryable}));
  return true;
});
chrome.tabs.onRemoved.addListener(tabId => chrome.storage.session.remove(`tab:${tabId}`));
