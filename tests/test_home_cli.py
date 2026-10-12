"""Homepage flows use the existing reader without taking over an unfinished login."""

import io
import json
from dataclasses import replace
from unittest.mock import MagicMock, Mock

import pytest
from rich.console import Console

from medium_reader import cli
from medium_reader.core import Article, MediumClient, ReaderError
from medium_reader.storage import Store


def story(number):
    return Article(
        f"Story {number}",
        f"https://medium.com/@author/story-{number:012x}",
        markdown=f"Available article body {number}.",
        source="page",
    )


@pytest.fixture
def app(tmp_path):
    cache = Store(tmp_path)
    client = Mock(spec=MediumClient)
    client.read.side_effect = AssertionError("Unexpected online article request")
    client.search.side_effect = AssertionError("Unexpected topic request")
    instance = cli.App(client, cache, Console(file=io.StringIO(), width=120))
    try:
        yield instance
    finally:
        cache.close()


def mock_home(monkeypatch, batches=()):
    feed = MagicMock()
    feed.__enter__.return_value = feed
    feed.__exit__.return_value = False
    feed.load_more.side_effect = list(batches)
    feed.space_id = 73
    feed.can_control = True
    factory = Mock(return_value=feed)
    feed.closed = False
    feed.connected = False
    monkeypatch.setattr(cli, "ChromeFeed", factory)
    return factory, feed


def prompt_answers(monkeypatch, *answers):
    ask = Mock(side_effect=answers)
    monkeypatch.setattr(cli.Prompt, "ask", ask)
    return ask


def displayed_pages(monkeypatch):
    pages = []

    def capture(console, topic, articles, offline, **kwargs):
        pages.append((topic, kwargs.get("start", 1), list(articles)))

    monkeypatch.setattr(cli, "show_results", capture)
    return pages


def test_offline_home_and_browse_never_construct_browser(app, monkeypatch):
    articles = [story(1), story(2), story(3)]
    app.store.remember_home(articles)
    browser = Mock(side_effect=AssertionError("Offline browsing must not open ego-lite"))
    monkeypatch.setattr(cli, "ChromeFeed", browser)

    assert app.home(limit=2, offline=True) == articles[:2]
    assert app.store.result(2) == articles[1]
    prompt_answers(monkeypatch, "n", "b")
    pages = displayed_pages(monkeypatch)
    app.browse_home(offline=True, page_size=2)

    browser.assert_not_called()
    assert [(start, cards) for _, start, cards in pages] == [
        (1, articles[:2]),
        (3, articles[2:]),
    ]
    assert app.store.home() == articles
    assert app.store.result(3) == articles[2]


def test_offline_home_json_keeps_numbers_usable_for_read(tmp_path, monkeypatch, capsys):
    cache = Store(tmp_path)
    articles = [story(1), story(2)]
    cache.remember_home(articles)
    cache.close()
    browser = Mock(side_effect=AssertionError("Offline CLI must not open ego-lite"))
    monkeypatch.setattr(cli, "ChromeFeed", browser)
    client = Mock(spec=MediumClient)
    client.read.side_effect = AssertionError("Offline CLI must not fetch an article")
    monkeypatch.setattr(cli, "MediumClient", Mock(return_value=client))

    assert cli.main(["--data-dir", str(tmp_path), "home", "--offline", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [(item["number"], item["title"]) for item in data] == [(1, "Story 1"), (2, "Story 2")]
    assert all("markdown" not in item for item in data)
    assert (
        cli.main(["--data-dir", str(tmp_path), "read", "2", "--offline", "--format", "json"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["markdown"] == articles[1].markdown
    browser.assert_not_called()
    client.read.assert_not_called()


def test_next_previous_and_read_keep_global_numbers_and_current_page(app, monkeypatch):
    articles = [story(number) for number in range(1, 6)]
    _, feed = mock_home(monkeypatch, [articles])
    pages = displayed_pages(monkeypatch)
    show_article = Mock()
    monkeypatch.setattr(cli, "show_article", show_article)
    prompt_answers(monkeypatch, "n", "3", "p", "n", "b")

    app.browse_home(page_size=2)

    assert [(start, cards) for _, start, cards in pages] == [
        (1, articles[:2]),
        (3, articles[2:4]),
        (3, articles[2:4]),
        (1, articles[:2]),
        (3, articles[2:4]),
    ]
    show_article.assert_called_once_with(app.console, articles[2], "rich", pager=True)
    feed.load_more.assert_called_once()
    assert [app.store.result(number) for number in range(1, 6)] == articles


def test_fetching_next_page_appends_new_stories_without_renumbering(app, monkeypatch):
    articles = [story(number) for number in range(1, 5)]
    duplicate = replace(articles[1], url=articles[1].url + "?source=home")
    _, feed = mock_home(monkeypatch, [articles[:2], [duplicate, *articles[2:], articles[2]]])
    pages = displayed_pages(monkeypatch)
    show_article = Mock()
    monkeypatch.setattr(cli, "show_article", show_article)
    prompt_answers(monkeypatch, "n", "3", "b")

    app.browse_home(page_size=2)

    assert [(start, cards) for _, start, cards in pages] == [
        (1, articles[:2]),
        (3, articles[2:]),
        (3, articles[2:]),
    ]
    assert feed.load_more.call_count == 2
    assert app.store.home() == articles
    assert [app.store.result(number) for number in range(1, 5)] == articles
    show_article.assert_called_once_with(app.console, articles[2], "rich", pager=True)


@pytest.mark.parametrize("browse", [False, True])
def test_failed_initial_load_preserves_current_result_mapping(app, monkeypatch, browse):
    previous = [story(9), story(10)]
    app.remember_home(previous)
    mock_home(monkeypatch, [ReaderError("Homepage unavailable")])

    with pytest.raises(ReaderError, match="Homepage unavailable"):
        if browse:
            app.browse_home()
        else:
            app.home()

    assert app.store.home() == previous
    assert [app.store.result(1), app.store.result(2)] == previous


def test_failed_next_load_preserves_already_displayed_results(app, monkeypatch):
    articles = [story(1), story(2)]
    mock_home(monkeypatch, [articles, ReaderError("Lost homepage connection")])
    prompt_answers(monkeypatch, "n")

    with pytest.raises(ReaderError, match="Lost homepage connection"):
        app.browse_home(page_size=2)

    assert app.store.home() == articles
    assert [app.store.result(1), app.store.result(2)] == articles


def test_logout_clears_persistent_browser_and_saved_result_state(app, monkeypatch):
    app.store.set_state("home_browser_space", 73)
    app.store.set_state("home_login_space", 74)
    app.store.remember_home([story(1)])
    app.store.remember_favorites([story(2)])
    factory, feed = mock_home(monkeypatch)
    assert app.chrome is feed

    app.logout()

    factory.assert_called_once_with()
    feed.close.assert_called_once_with()
    feed.logout.assert_not_called()
    assert app.store.state("home_browser_space") == 73  # Legacy ego-lite session stays untouched.
    assert app.store.state("home_login_space") == 74
    assert app.store.state("favorite_results") == []
    assert app.store.home() == []


def test_sorting_uses_available_dates_and_clap_counts(app):
    newest = replace(story(1), published="2026-10-09", claps=200)
    popular = replace(story(2), published="2026-10-08", claps=900)
    unknown = replace(story(3), published="", claps=None)

    assert app._sort_articles([unknown, popular, newest], "recent") == [
        newest,
        popular,
        unknown,
    ]
    assert app._sort_articles([newest, popular], "popular") == [popular, newest]
    with pytest.raises(ReaderError, match="clap counts"):
        app._sort_articles([story(4)], "popular")


def test_topic_results_page_with_next_previous_and_stop_message(app, monkeypatch):
    articles = [story(number) for number in range(1, 4)]
    pages = displayed_pages(monkeypatch)
    prompt_answers(monkeypatch, "n", "n", "p", "b")

    app.browse_results("python", articles, page_size=2)

    assert [(start, batch) for _, start, batch in pages] == [
        (1, articles[:2]),
        (3, articles[2:]),
        (3, articles[2:]),
        (1, articles[:2]),
    ]


def test_home_then_topic_uses_existing_search_and_reader_flow(app, monkeypatch):
    homepage = [story(1)]
    topic_results = [story(2)]
    factory, _ = mock_home(monkeypatch, [homepage])
    app.client.search.side_effect = None
    app.client.search.return_value = topic_results
    pages = displayed_pages(monkeypatch)
    prompt_answers(monkeypatch, "/home", "b", "Machine Learning", "b", "q")

    app.interactive()

    factory.assert_called_once_with()
    app.client.search.assert_called_once_with("machine-learning")
    assert pages == [
        ("Your homepage", 1, homepage),
        ("machine-learning", 1, topic_results),
    ]
    assert app.store.home() == homepage
    assert app.store.topic("machine-learning") == topic_results
    assert app.store.result(1) == topic_results[0]


def test_login_opens_browser_and_waits_for_confirmation(app, monkeypatch):
    factory, feed = mock_home(monkeypatch)
    articles = [story(1)]
    feed.confirm_login.return_value = articles
    prompt_answers(monkeypatch, "done")
    app.login()
    factory.assert_called_once_with()
    feed.begin_login.assert_called_once_with()
    feed.confirm_login.assert_called_once_with()
    assert app.store.home() == articles


def test_login_opens_browser_even_if_previously_connected(app, monkeypatch):
    factory, feed = mock_home(monkeypatch)
    articles = [story(3)]
    feed.connected = True
    feed.confirm_login.return_value = articles
    prompt_answers(monkeypatch, "done")
    app.login()
    factory.assert_called_once_with()
    feed.begin_login.assert_called_once_with()
    feed.confirm_login.assert_called_once_with()
    assert app.store.home() == articles


def test_canceled_login_does_not_confirm_connection(app, monkeypatch):
    _, feed = mock_home(monkeypatch)
    prompt_answers(monkeypatch, "q")
    app.login()
    feed.confirm_login.assert_not_called()
    feed.load_more.assert_not_called()
    feed.begin_login.assert_called_once_with()


def test_failed_login_does_not_store_unverified_feed(app, monkeypatch):
    _, feed = mock_home(monkeypatch)
    feed.confirm_login.side_effect = ReaderError("Sign-in is still in progress")
    prompt_answers(monkeypatch, "done")
    with pytest.raises(ReaderError, match="Sign-in is still in progress"):
        app.login()
    assert app.store.state("home_results") is None


def test_partial_page_fills_before_advancing_so_new_cards_are_not_skipped(app, monkeypatch):
    articles = [story(number) for number in range(1, 5)]
    mock_home(monkeypatch, [articles[:2], articles])
    pages = displayed_pages(monkeypatch)
    prompt_answers(monkeypatch, "n", "n", "b")
    app.browse_home(page_size=3)
    assert [(start, cards) for _, start, cards in pages] == [
        (1, articles[:2]),
        (1, articles[:3]),
        (4, articles[3:]),
    ]


def test_favorites_sort_updates_result_numbers_before_reading(app, monkeypatch):
    first = replace(story(1), claps=10)
    popular = replace(story(2), claps=100)
    _, feed = mock_home(monkeypatch)
    feed.load_favorites.return_value = [first, popular]
    displayed_pages(monkeypatch)
    show = Mock()
    monkeypatch.setattr(cli, "show_article", show)
    prompt_answers(monkeypatch, "/popular", "1", "b")
    app.browse_favorites()
    assert show.call_args.args[1] == popular


def test_offline_home_sorts_before_limiting(app):
    old = replace(story(1), published="2026-10-01")
    new = replace(story(2), published="2026-10-11")
    app.store.remember_home([old, new])
    assert app.home(limit=1, offline=True)[0] == new


def test_empty_popularity_sort_is_valid(app):
    assert app._sort_articles([], "popular") == []


def test_slash_command_aliases_work_after_global_options(tmp_path, capsys):
    assert cli.main(["--data-dir", str(tmp_path), "/logout"]) == 0
    assert "disconnected" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["home", "favorites"])
def test_one_shot_live_json_connects_in_browser_without_polluting_stdout(
    tmp_path, monkeypatch, capsys, command
):
    _, feed = mock_home(monkeypatch, [[story(1)]])
    feed.load_favorites.return_value = [story(1)]
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    argv = ["--data-dir", str(tmp_path), command, "--json"]
    if command == "home":
        argv += ["--limit", "1"]
    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)[0]["title"] == "Story 1"
    assert "Opening your browser" in captured.err
    feed.begin_login.assert_called_once_with()
    feed.wait_for_login.assert_called_once_with()
    feed.close.assert_called_once_with()


def test_unattended_live_cli_does_not_open_browser_or_wait_for_login(tmp_path, monkeypatch, capsys):
    _, feed = mock_home(monkeypatch)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.main(["--data-dir", str(tmp_path), "home", "--json"]) == 1
    assert capsys.readouterr().out == ""
    feed.begin_login.assert_not_called()
    feed.wait_for_login.assert_not_called()


def test_offline_topic_picker_cannot_fetch_article_online(app, monkeypatch):
    item = replace(story(1), source="rss-preview")
    app.store.remember_topic("python", [item])
    prompt_answers(monkeypatch, "1", "b")
    monkeypatch.setattr(cli, "show_article", Mock())
    app.browse_results("python", [item], offline=True)
    app.client.read.assert_not_called()


def test_home_restores_result_numbers_after_saved_feed_returns(app, monkeypatch):
    home, saved = story(1), story(2)
    _, feed = mock_home(monkeypatch, [[home]])
    feed.load_favorites.return_value = [saved]
    prompt_answers(monkeypatch, "/favorites", "b", "1", "b")
    monkeypatch.setattr(cli, "show_article", show := Mock())
    app.browse_home()
    assert show.call_args.args[1] == home


def test_loaded_homepage_keeps_popularity_sort_for_new_cards(app, monkeypatch):
    first = replace(story(1), claps=20, published="2026-10-11")
    popular = replace(story(2), claps=100, published="2026-10-01")
    incoming = replace(story(3), claps=50, published="2026-10-10")
    mock_home(monkeypatch, [[first, popular], [incoming]])
    prompt_answers(monkeypatch, "/popular", "n", "b")
    displayed_pages(monkeypatch)
    app.browse_home(page_size=2)
    assert app.store.result(1) == popular
    assert app.store.result(2) == incoming
