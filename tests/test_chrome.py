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


def pair(client):
    reply = client.post("/pair", json={})
    if reply.status_code == 200:
        client.headers["X-Medium-Reader-Session"] = reply.json()["session"]
    return reply


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
    assert pair(client).status_code == 200
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
    assert snapshot(client).status_code == 403
    assert pair(client).status_code == 200
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
    pair(client)
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
    pair(client)
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
    pair(client)
    with pytest.raises(ReaderError, match="did not respond"):
        feed.load_more()
    assert feed.command is None
    feed.close()
    assert feed.closed and feed.server is None and not feed.thread.is_alive()


def test_stale_chrome_login_is_not_connected(bridge):
    feed, client = bridge
    pair(client)
    snapshot(client)
    feed.snapshot["received"] -= chrome.HEARTBEAT_GRACE + 1
    assert not feed.connected
    with pytest.raises(SignInRequired):
        feed.confirm_login()


def test_reconnecting_revokes_previous_tab_session_and_account_snapshot(bridge):
    feed, client = bridge
    pair(client)
    old_session = client.headers["X-Medium-Reader-Session"]
    snapshot(client)
    assert feed.connected
    pair(client)
    assert client.headers["X-Medium-Reader-Session"] != old_session
    assert not feed.connected
    assert (
        client.get("/command", headers={"X-Medium-Reader-Session": old_session}).status_code == 403
    )
    assert (
        client.post(
            "/snapshot", json={"html": CARD}, headers={"X-Medium-Reader-Session": old_session}
        ).status_code
        == 403
    )
    assert snapshot(client).status_code == 200
    assert feed.connected


@pytest.mark.parametrize(
    "extra",
    [
        {"signedIn": "true"},
        {"empty": "true"},
        {"error": []},
        {"id": {}},
        {"url": "https://user:secret@medium.com/"},
        {"url": "https://medium.com:8443/"},
    ],
)
def test_snapshot_rejects_bad_types_and_unsafe_page_urls(bridge, extra):
    feed, client = bridge
    pair(client)
    assert snapshot(client, **extra).status_code == 400
    assert feed.snapshot is None


def test_browser_launch_failure_does_not_leave_a_pending_login_command(bridge, monkeypatch):
    feed, _ = bridge
    monkeypatch.setattr(chrome.webbrowser, "open", Mock(return_value=False))
    with pytest.raises(ReaderError, match="Could not open"):
        feed.begin_login()
    assert not feed.opened and feed.command is None


def test_login_confirmation_reuses_verified_homepage_snapshot(bridge):
    feed, client = bridge
    pair(client)
    snapshot(client)
    assert feed.confirm_login()[0].title == "Chrome story"
    assert feed.command is None


def test_new_login_gate_cannot_be_completed_by_the_previous_chrome_tab(bridge, monkeypatch):
    feed, client = bridge
    pair(client)
    snapshot(client)
    old_session = client.headers["X-Medium-Reader-Session"]
    monkeypatch.setattr(chrome.webbrowser, "open", Mock(return_value=True))
    feed.begin_login()
    assert not feed.connected
    assert (
        client.get("/command", headers={"X-Medium-Reader-Session": old_session}).status_code == 403
    )
    pair(client)
    snapshot(client)
    assert feed.connected


def test_background_tab_heartbeat_tolerates_chrome_timer_throttling(bridge):
    feed, client = bridge
    pair(client)
    snapshot(client)
    feed.snapshot["received"] -= 60
    assert feed.connected
    assert feed.state == "connected"


def test_closed_bridge_is_not_still_marked_connected(bridge):
    feed, client = bridge
    pair(client)
    snapshot(client)
    feed.close()
    assert not feed.connected and feed.state == "disconnected"


def test_wait_for_login_wakes_on_browser_confirmation_and_times_out_cleanly(bridge):
    feed, client = bridge
    feed.login_timeout = 0.2
    with pytest.raises(ReaderError, match="connection timed out"):
        feed.wait_for_login()
    with ThreadPoolExecutor() as executor:
        result = executor.submit(feed.wait_for_login)
        pair(client)
        snapshot(client)
        assert result.result(timeout=1) is None


def test_wait_for_login_reports_browser_errors_without_waiting_for_timeout(bridge):
    feed, client = bridge
    pair(client)
    snapshot(client, signed_in=False, error="Medium's account controls have not loaded")
    with pytest.raises(ReaderError, match="account controls"):
        feed.wait_for_login()


def test_home_and_saved_requests_do_not_overwrite_each_other(bridge):
    feed, client = bridge
    feed.timeout = 1
    pair(client)
    with ThreadPoolExecutor() as executor:
        first = executor.submit(feed._request, "home")
        with feed.condition:
            assert feed.condition.wait_for(lambda: feed.command, timeout=1)
            request_id = feed.command["id"]
        second = executor.submit(feed._request, "favorites")
        assert client.get("/command").json()["action"] == "home"
        snapshot(client, request_id=request_id)
        first.result(timeout=1)
        with feed.condition:
            assert feed.condition.wait_for(lambda: feed.command, timeout=1)
            request = dict(feed.command)
        assert request["action"] == "favorites"
        snapshot(client, request_id=request["id"])
        second.result(timeout=1)
