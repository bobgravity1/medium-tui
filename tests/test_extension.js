const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const extension = path.join(__dirname, "../src/medium_reader/chrome_extension");

function mediumHarness({pathname = "/", signedIn = false, signedOut = false, reply = {}} = {}) {
  const messages = [];
  const navigation = [];
  const timers = [];
  let now = 0;
  const account = {getClientRects: () => [1], getAttribute: () => "user options menu"};
  const signIn = {getClientRects: () => [1], textContent: "Sign in"};
  const root = {textContent: "", querySelectorAll: () => []};
  const context = vm.createContext({
    module: {exports: {}}, URL,
    Date: {now: () => now},
    location: {pathname, href: `https://medium.com${pathname}`, origin: "https://medium.com",
               assign: url => navigation.push(url)},
    document: {
      body: root,
      querySelector: selector => selector === "main" ? root : null,
      querySelectorAll: selector => {
        if (selector === "button, [role=button]") return signedIn ? [account] : [];
        if (selector === "a,button") return signedOut ? [signIn] : [];
        return [];
      },
      createElement: () => ({outerHTML: "<main></main>", append: () => {}})
    },
    chrome: {runtime: {sendMessage: async message => {
      messages.push(message);
      return message.kind === "command" ? reply : {ok: true};
    }}},
    setTimeout: (fn, ms) => {
      if (ms <= 200) {now += ms; fn();}
      else timers.push({fn, ms});
    },
    window: {scrollTo: () => {}}
  });
  vm.runInContext(fs.readFileSync(path.join(extension, "medium.js"), "utf8"), context);
  return {api: context.module.exports, messages, navigation, timers};
}

test("signed-out favorites do not navigate into a sign-in redirect loop", async () => {
  const h = mediumHarness({pathname: "/m/signin", signedOut: true});
  await h.api.perform({id: "favorite-job", action: "favorites"});
  assert.equal(h.navigation.length, 0);
  assert.equal(h.messages.at(-1).kind, "snapshot");
  assert.equal(h.messages.at(-1).data.signedIn, false);
  assert.equal(h.messages.at(-1).data.id, "favorite-job");
});

test("pending login reports sign-in state without acknowledging the login job", async () => {
  const h = mediumHarness({signedOut: true});
  await h.api.perform({id: "login-job", action: "login"});
  assert.equal(h.messages.at(-1)?.kind, "snapshot");
  assert.equal(h.messages.at(-1).data.signedIn, false);
  assert.equal(h.messages.at(-1).data.id, "");
  assert.match(h.navigation[0], /\/m\/signin/);
});

test("temporary connection failures retry instead of disabling the content script", async () => {
  const h = mediumHarness({reply: {error: "Temporary network failure", retryable: true}});
  await h.api.loop();
  assert.equal(h.timers.length, 1);
  assert.ok(h.timers[0].ms >= 1500);
});

test("an unpaired tab does not keep polling the reader", async () => {
  const h = mediumHarness({reply: {unpaired: true}});
  await h.api.loop();
  assert.equal(h.timers.length, 0);
});

test("a page that has not hydrated is not mistaken for a signed-out account", async () => {
  const h = mediumHarness();
  await h.api.perform({id: "login-job", action: "login"});
  assert.equal(h.navigation.length, 0);
  assert.ok(h.messages.at(-1)?.data.error);
});

function workerHarness({status = 200, failFetch = false} = {}) {
  const sessions = {};
  const requests = [];
  const navigations = [];
  let listener;
  let pairs = 0;
  const context = vm.createContext({
    URL, AbortController, setTimeout, clearTimeout,
    chrome: {
      storage: {session: {
        get: async key => key === null ? {...sessions} : {[key]: sessions[key]},
        set: async values => Object.assign(sessions, values),
        remove: async keys => {for (const key of [keys].flat()) delete sessions[key];}
      }},
      tabs: {update: async (id, data) => navigations.push({id, ...data}), onRemoved: {addListener: () => {}}},
      runtime: {onMessage: {addListener: callback => {listener = callback;}}}
    },
    fetch: async (url, options) => {
      requests.push({url, ...options});
      if (failFetch) throw new TypeError("Temporary network failure");
      return {ok: status === 200, status, json: async () =>
        url.endsWith("/pair") ? {ok: true, session: `session-${++pairs}`} : {ok: true}
      };
    }
  });
  vm.runInContext(fs.readFileSync(path.join(extension, "background.js"), "utf8"), context);
  return {handle: context.handle, sessions, requests, navigations,
          message: (data, sender) => new Promise(resolve => listener(data, sender, resolve))};
}

function localSender(id = 7) {
  return {frameId: 0, tab: {id}, url: "http://127.0.0.1:45678/connect"};
}
const pairMessage = {kind: "pair", endpoint: "http://127.0.0.1:45678", token: "a".repeat(43)};

test("pairing uses only a local page and never reads browser cookies", async () => {
  const h = workerHarness();
  await h.handle(pairMessage, localSender());
  assert.equal(h.sessions["tab:7"].session, "session-1");
  assert.equal(h.requests[0].credentials, "omit");
  assert.equal(h.navigations[0].url, "https://medium.com/");
  const calls = h.requests.length;
  await assert.rejects(h.handle(pairMessage, {...localSender(), url: "https://malicious.example/connect"}));
  assert.equal(h.requests.length, calls);
});

test("reconnecting removes previous sharing tabs for the same reader", async () => {
  const h = workerHarness();
  await h.handle(pairMessage, localSender(7));
  await h.handle(pairMessage, localSender(8));
  assert.equal(h.sessions["tab:7"], undefined);
  assert.equal(h.sessions["tab:8"].session, "session-2");
  const reply = await h.handle({kind: "command"}, {frameId: 0, tab: {id: 7}, url: "https://medium.com/"});
  assert.equal(reply.unpaired, true);
});

test("a Medium tab sends its session header and cannot submit a foreign page", async () => {
  const h = workerHarness();
  await h.handle(pairMessage, localSender());
  const sender = {frameId: 0, tab: {id: 7}, url: "https://medium.com/"};
  await h.handle({kind: "command"}, sender);
  assert.equal(h.requests.at(-1).headers["X-Medium-Reader-Session"], "session-1");
  await assert.rejects(h.handle({kind: "snapshot", data: {url: "https://malicious.example/", html: ""}}, sender));
  await assert.rejects(h.handle({kind: "command"}, {...sender, frameId: 1}));
});

test("transport failures are retryable but revoked sessions stop polling", async () => {
  const temporary = workerHarness({failFetch: true});
  const error = await temporary.message(pairMessage, localSender());
  assert.equal(error.retryable, true);
  const revoked = workerHarness({status: 403});
  const denial = await revoked.message(pairMessage, localSender());
  assert.equal(denial.retryable, false);
});

test("the extension does not ask for debugger, cookies, or history permissions", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(extension, "manifest.json"), "utf8"));
  assert.deepEqual(manifest.permissions, ["storage"]);
  assert.deepEqual(manifest.host_permissions, ["http://127.0.0.1/*"]);
});
