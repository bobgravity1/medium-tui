(() => {
  if (location.pathname !== "/connect" || !document.querySelector("#medium-reader-connect")) return;
  const token = location.hash.slice(1);
  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) return;
  const button = document.createElement("button");
  button.textContent = "Connect and open Medium";
  document.querySelector("#medium-reader-connect").append(button);
  button.addEventListener("click", async () => {
    button.disabled = true;
    const response = await chrome.runtime.sendMessage({
      kind: "pair", endpoint: location.origin, token
    }).catch(error => ({error: error.message}));
    if (response?.error) {
      document.querySelector("#connection").textContent = response.error;
      button.disabled = false;
    }
  });
})();
