(() => {
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = node => node && node.getClientRects().length > 0;
  const signedIn = () => [...document.querySelectorAll("button, [role=button]")].some(node =>
    visible(node) && /user options menu|account menu/i.test(node.getAttribute("aria-label") || "")
  );
  const signedOut = () => [...document.querySelectorAll("a,button")].some(node =>
    visible(node) && /^sign in$/i.test(node.textContent.trim())
  ) || location.pathname.startsWith("/m/signin");

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
    if (result?.error) throw new Error(result.error);
  }

  async function perform(command) {
    const home = "https://medium.com/";
    if (command.action === "login") {
      await waitFor(() => signedIn() || signedOut());
      if (!signedIn()) {
        if (!location.pathname.startsWith("/m/signin")) {
          location.assign("https://medium.com/m/signin?redirect=https%3A%2F%2Fmedium.com%2F");
        }
        // Leave the login job pending so finishing sign-in completes it automatically.
        return;
      }
      if (location.pathname !== "/") { location.assign(home); return; }
    } else if (command.action === "home" || command.action === "more") {
      if (location.pathname !== "/") { location.assign(home); return; }
    } else if (command.action === "favorites") {
      if (location.pathname === "/me/lists") {
        const found = await waitFor(() => document.querySelector('a[href*="/list/reading-list"]') || signedOut());
        const list = document.querySelector('a[href*="/list/reading-list"]');
        if (list) { location.assign(list.href); return; }
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
      if (command?.unpaired || command?.error) return;
      if (command?.id) await perform(command);
      else await send(capture());
    } catch (_) { return; } // A closed CLI stops sharing automatically.
    setTimeout(loop, 1500);
  }
  loop();
})();
