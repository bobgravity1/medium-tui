(() => {
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = node => node && node.getClientRects().length > 0;
  const signedIn = () => [...document.querySelectorAll("button, [role=button]")].some(node =>
    visible(node) && /user options menu|account menu/i.test(node.getAttribute("aria-label") || "")
  );
  const signedOut = () => [...document.querySelectorAll("a,button")].some(node =>
    visible(node) && /^sign in$/i.test(node.textContent.trim())
  ) || location.pathname.startsWith("/m/signin");
  let failures = 0;

  async function waitFor(test, timeout = 20000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (test()) return true;
      await delay(200);
    }
    return false;
  }

  function capture(id = "", error = "") {
    // Export card elements only, not document state, forms, scripts, or cookies.
    const root = document.querySelector("main") || document.body;
    const cards = document.createElement("main");
    for (const node of root.querySelectorAll('article, [data-testid="post-preview"]')) {
      const clone = node.cloneNode(true);
      clone.querySelectorAll("script,style,form,input,textarea,iframe,[hidden]").forEach(n => n.remove());
      cards.append(clone);
    }
    const empty = /no stories|add your favorite stories to your list/i.test(root.textContent);
    return {id, url: location.href, signedIn: signedIn(), html: cards.outerHTML, empty, error};
  }

  async function send(data) {
    const result = await chrome.runtime.sendMessage({kind: "snapshot", data});
    if (result?.error) {
      const error = new Error(result.error);
      error.retryable = !!result.retryable;
      throw error;
    }
  }

  async function perform(command) {
    const home = "https://medium.com/";
    // Decide authentication before navigating. A signed-out favorites request
    // must not repeatedly bounce from /m/signin to /me/lists and back.
    await waitFor(() => signedIn() || signedOut(), 5000);
    if (!signedIn() && !signedOut()) {
      await send(capture(command.id, "Medium's account controls have not loaded. Refresh the connected tab and retry."));
      return;
    }
    if (!signedIn()) {
      await send(capture(command.action === "login" ? "" : command.id));
      if (command.action === "login" && !location.pathname.startsWith("/m/signin")) {
        location.assign("https://medium.com/m/signin?redirect=https%3A%2F%2Fmedium.com%2F");
      }
      return;
    }
    if (command.action === "login") {
      if (location.pathname !== "/") { location.assign(home); return; }
    } else if (command.action === "home" || command.action === "more") {
      if (location.pathname !== "/") { location.assign(home); return; }
    } else if (command.action === "favorites") {
      if (location.pathname === "/me/lists") {
        const found = await waitFor(() => document.querySelector('a[href*="/list/reading-list"]') || signedOut());
        const list = document.querySelector('a[href*="/list/reading-list"]');
      if (list) {
        const url = new URL(list.href);
        if (url.origin !== "https://medium.com") {
          await send(capture(command.id, "The Reading list link is outside Medium.")); return;
        }
        location.assign(url.href); return;
      }
        await send(capture(command.id, found && signedOut() ? "" : "Your Reading list link was not found."));
        return;
      }
      if (!location.pathname.includes("/list/reading-list")) {
        location.assign("https://medium.com/me/lists"); return;
      }
    } else {
      await send(capture(command.id, "Unknown reader request.")); return;
    }
    if (command.action === "more") {
      const before = document.querySelectorAll('article, [data-testid="post-preview"]').length;
      window.scrollTo({top: document.body.scrollHeight, behavior: "instant"});
      await waitFor(() => document.querySelectorAll('article, [data-testid="post-preview"]').length > before, 5000);
    }
    await waitFor(() => signedOut() || (signedIn() && (
      document.querySelector('article, [data-testid="post-preview"]') ||
      /no stories|add your favorite stories to your list/i.test(document.body.textContent)
    )));
    await send(capture(command.id));
  }

  async function loop() {
    try {
      const command = await chrome.runtime.sendMessage({kind: "command"});
      if (command?.unpaired) return;
      if (command?.error) {
        if (!command.retryable) return;
        const error = new Error(command.error);
        error.retryable = true;
        throw error;
      }
      if (command?.id) await perform(command);
      else await send(capture());
      failures = 0;
    } catch (error) {
      if (error.retryable === false || ++failures >= 8) return;
    }
    // Brief transport glitches should not permanently disable the installed tab.
    // Back off, then stop after repeated failures when a reader is no longer running.
    setTimeout(loop, Math.min(10000, 1500 * 2 ** failures));
  }
  if (typeof module !== "undefined") module.exports = {perform, loop, capture};
  else loop();
})();
