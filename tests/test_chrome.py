"""Exercise the actual loopback bridge without launching or signing out a browser."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import httpx
import pytest

from medium_reader import chrome
from medium_reader.chrome import ChromeFeed, SignInRequired
from medium_reader.core import ReaderError

ORIGIN = "chrome-extension://" + "a" * 32
CARD = (
    '<main><article><a href="/@sample/story-123456abcdef">'
    "<h2>Chrome story</h2></a></article></main>"
)


@pytest.fixture
def bridge():
    feed = ChromeFeed(timeout=0.2)
    feed._start()
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{feed.server.server_port}",
        headers={"Authorization": "Bearer " + feed.token, "Origin": ORIGIN},
        trust_env=False,
    )
    try:
        yield feed, client
    finally:
        client.close()
        feed.close()


def snapshot(client, *, signed_in=True, request_id="", **extra):
    return client.post(
        "/snapshot",
        json={
            "url": "https://medium.com/",
            "signedIn": signed_in,
            "html": CARD,
            "id": request_id,
            **extra,
        },
    )


def test_login_opens_default_browser_but_does_not_imply_authentication(bridge, monkeypatch):
    feed, client = bridge
    opener = Mock(return_value=True)
    monkeypatch.setattr(chrome.webbrowser, "open", opener)
    feed.begin_login()
    address = opener.call_args.args[0]
    assert address.startswith(f"http://127.0.0.1:{feed.server.server_port}/connect#")
    assert address.endswith(feed.token)
    assert client.get("/connect").status_code == 200
    assert not feed.connected
    assert client.post("/pair", json={}).status_code == 200
    assert snapshot(client, signed_in=False).status_code == 200
    assert not feed.connected
    assert snapshot(client).status_code == 200
    assert feed.connected


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer wrong"},
        {"Origin": "https://medium.com"},
        {"Origin": "https://malicious.example"},
        {"Host": "malicious.example"},
    ],
)
def test_bridge_rejects_bad_tokens_web_origins_and_rebinding(bridge, headers):
    feed, client = bridge
    assert client.post("/pair", json={}, headers=headers).status_code == 403
    assert not feed.paired


def test_bridge_pins_extension_origin_and_rejects_unpaired_or_bad_payloads(bridge):
    feed, client = bridge
    assert snapshot(client).status_code == 404
    assert client.post("/pair", json={}).status_code == 200
    assert (
        client.get("/command", headers={"Origin": "chrome-extension://" + "b" * 32}).status_code
        == 403
    )
    for url in ("https://malicious.example/", [], "https://[broken"):
        assert snapshot(client, url=url).status_code == 400
    assert client.post("/snapshot", content="not json").status_code == 400
    assert client.post("/snapshot", content="x" * (chrome.MAX_SNAPSHOT + 1)).status_code == 413
    assert feed.snapshot is None


def test_live_feed_waits_for_matching_chrome_reply_and_parses_cards(bridge):
    feed, client = bridge
    client.post("/pair", json={})
    with ThreadPoolExecutor() as executor:
        result = executor.submit(feed.load_more)
        with feed.condition:
            assert feed.condition.wait_for(lambda: feed.command is not None, timeout=1)
            request = dict(feed.command)
        assert request["action"] == "home"
        snapshot(client, request_id="stale-reply")
        assert not result.done()
        snapshot(client, request_id=request["id"])
        assert result.result(timeout=1)[0].title == "Chrome story"
    assert feed.loaded


def test_logged_out_chrome_returns_a_login_gate_not_a_fake_feed(bridge):
    feed, client = bridge
    client.post("/pair", json={})
    with ThreadPoolExecutor() as executor:
        result = executor.submit(feed.load_more)
        with feed.condition:
            assert feed.condition.wait_for(lambda: feed.command is not None, timeout=1)
            request_id = feed.command["id"]
        snapshot(client, signed_in=False, request_id=request_id)
        with pytest.raises(SignInRequired):
            result.result(timeout=1)
    assert not feed.loaded


def test_closed_tab_times_out_and_disconnect_releases_listener(bridge):
    feed, client = bridge
    with pytest.raises(ReaderError, match="not connected"):
        feed.load_more()
    client.post("/pair", json={})
    with pytest.raises(ReaderError, match="did not respond"):
        feed.load_more()
    assert feed.command is None
    feed.close()
    assert feed.closed and feed.server is None and not feed.thread.is_alive()


def test_stale_chrome_login_is_not_connected(bridge):
    feed, client = bridge
    client.post("/pair", json={})
    snapshot(client)
    feed.snapshot["received"] -= 11
    assert not feed.connected
    with pytest.raises(SignInRequired):
        feed.confirm_login()
