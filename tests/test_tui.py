"""Exercise real key events/layout with Textual's headless terminal driver."""

import asyncio
from argparse import Namespace
from io import StringIO
from unittest.mock import Mock

import pytest
from rich.console import Console
from textual.containers import VerticalScroll
from textual.widgets import Input, OptionList, Static

from medium_reader.cli import App
from medium_reader.core import Article, MediumClient, ReaderError
from medium_reader.storage import Store
from medium_reader.tui import ReaderUI, article_body, section_progress


def story(number=1):
    return Article(
        title=f"Story {number}",
        url=f"https://medium.com/@sample/story-{number:012x}",
        author="Sample author",
        published=f"2026-10-{number:02}",
        source="page",
        markdown=(
            "## Opening\n\n"
            + "A readable paragraph with some useful context.\n\n" * 24
            + "## Second section\n\n"
            + "More useful context.\n\n" * 24
            + "### Details\n\n```python\n# Not an outline heading\nprint('hello')\n```\n"
            + "## Conclusion\n\n"
            + "A final thought.\n\n" * 24
        ),
        claps=number * 100,
    )


@pytest.fixture
def backend(tmp_path):
    store = Store(tmp_path)
    client = Mock(spec=MediumClient)
    client.search.side_effect = AssertionError("Unexpected network call")
    client.read.side_effect = AssertionError("Unexpected network call")
    app = App(client, store, Console(file=StringIO()))
    yield app
    app.close()
    store.close()


def test_reader_heading_jump_resize_back_and_resume(backend):
    async def exercise():
        items = [story(1), story(2)]
        backend.store.remember_home(items)
        ui = ReaderUI(backend, args=Namespace(command="home", offline=True))
        async with ui.run_test(size=(120, 40)) as pilot:
            await ui.workers.wait_for_complete()
            await pilot.pause()
            assert ui.articles[0].title == "Story 2"
            assert ui.query_one("#navigation").display
            await pilot.press("down", "enter")
            await ui.workers.wait_for_complete()
            await pilot.pause()
            assert ui.reading and ui.article.title == "Story 1"
            assert len(ui.heading_rows) == 5  # Code-block '#' stays out of the outline.
            outline = ui.query_one("#outline", OptionList)
            outline.focus()
            outline.highlighted = 2
            await pilot.press("enter")
            await pilot.pause()
            reader = ui.query_one("#reader", VerticalScroll)
            assert reader.scroll_y > 0
            assert ui.active_section == 2
            ui.save_position()
            assert 0 < ui.saved_fraction or reader.scroll_y > 0
            await pilot.resize_terminal(60, 28)
            await pilot.pause()
            assert not ui.query_one("#sidebar").display
            assert reader.size.width <= 60
            await pilot.resize_terminal(120, 40)
            await pilot.press("escape")
            await pilot.pause()
            assert not ui.reading
            assert ui.query_one("#stories", OptionList).highlighted == 1
            await pilot.press("enter")
            await ui.workers.wait_for_complete()
            await pilot.pause()
            assert reader.scroll_y > 0  # Reopened at the saved place.
            await pilot.press("G")
            await pilot.pause()
            assert ui.last_progress[0] == 100
            assert all(value == 100 for value in ui.last_progress[2])

    asyncio.run(exercise())


def test_topic_commands_empty_results_errors_and_offline_read(backend):
    async def exercise():
        backend.store.remember_topic("python", [story(1), story(2)])
        ui = ReaderUI(backend, args=Namespace(command="search", topic=["python"], offline=True))
        async with ui.run_test(size=(100, 34)) as pilot:
            await ui.workers.wait_for_complete()
            await pilot.pause()
            command = ui.query_one("#command", Input)
            await pilot.press("slash")
            assert command.has_focus and command.value == "/"
            command.value = "/popular"
            await pilot.press("enter")
            await ui.workers.wait_for_complete()
            assert ui.articles[0].title == "Story 2"
            await pilot.press("enter")
            await ui.workers.wait_for_complete()
            await pilot.pause()
            assert ui.reading and ui.article.title == "Story 2"
            await pilot.press("escape")
            backend.store.remember_topic("empty", [])
            await ui.perform("search", "empty")
            assert ui.query_one("#empty").display
            assert not ui.query_one("#stories").display
            with pytest.raises(ReaderError):
                await ui.perform("search", "never-cached")
            assert ui.topic == "empty"
            backend.client.search.assert_not_called()
            backend.client.read.assert_not_called()

    asyncio.run(exercise())


def test_home_load_more_and_login_handoff(backend, monkeypatch):
    from medium_reader import cli

    feed = Mock()
    feed.closed = False
    feed.connected = False
    feed.load_more.side_effect = [[story(1)], [story(1), story(2)]]
    feed.confirm_login.return_value = [story(1)]
    monkeypatch.setattr(cli, "ChromeFeed", Mock(return_value=feed))

    async def exercise():
        ui = ReaderUI(backend)
        async with ui.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            await ui.perform("login", "")
            feed.begin_login.assert_called_once()
            feed.confirm_login.assert_not_called()
            assert ui.login_pending
            feed.connected = True
            ui.check_connection()
            await ui.workers.wait_for_complete()
            assert not ui.login_pending
            assert ui.reading is False and ui.articles
            await ui.perform("home", "")
            await ui.perform("more", "")
            assert len(ui.articles) == 2
            assert backend.store.result(1).title == "Story 2"
            await ui.perform("logout", "")
            feed.close.assert_called_once()
            feed.logout.assert_not_called()
            assert not ui.articles

    asyncio.run(exercise())


def test_short_article_and_no_headings(backend):
    async def exercise():
        item = story()
        item.markdown = "A single paragraph."
        ui = ReaderUI(backend, article=item)
        async with ui.run_test(size=(90, 30)) as pilot:
            await pilot.pause()
            assert len(ui.heading_rows) == 1
            assert ui.last_progress[0] == 100

    asyncio.run(exercise())


def test_title_deduplication_and_section_bounds():
    item = story()
    item.markdown = "# Story 1\n\nBody"
    assert article_body(item) == "Body"
    assert section_progress(25, [0, 10, 40], 80) == [1, 0.5, 0]


def test_paragraph_click_copies_full_plain_text_and_shows_feedback(backend, monkeypatch):
    from medium_reader import tui

    copier = Mock(return_value=True)
    monkeypatch.setattr(tui, "copy_text", copier)

    async def exercise():
        item = story()
        item.markdown = (
            "A **whole paragraph** with `code` and enough text to wrap.\n\nSecond paragraph."
        )
        ui = ReaderUI(backend, article=item)
        async with ui.run_test(size=(90, 30)) as pilot:
            await pilot.pause()
            paragraph = ui.query("MarkdownParagraph").first()
            await pilot.click(paragraph, offset=(4, 0))
            await pilot.pause()
            copier.assert_called_once_with("A whole paragraph with code and enough text to wrap.")
            assert paragraph.has_class("copied-paragraph")
            assert str(ui.query_one("#copy-status", Static).renderable) == "Copied"
            ui.clear_copy_feedback()
            assert not paragraph.has_class("copied-paragraph")
            assert str(ui.query_one("#copy-status", Static).renderable) == ""
            await pilot.click("#view-title")
            copier.assert_called_once()

    asyncio.run(exercise())


def test_stale_ego_login_shows_chrome_gate_without_opening_browser(backend):
    async def exercise():
        backend.store.set_state("home_browser_space", 2)
        backend.store.set_state("home_login_space", 2)
        ui = ReaderUI(backend)
        async with ui.run_test(size=(100, 34)) as pilot:
            await pilot.pause()
            assert ui.query_one("#empty").display
            assert "Connect" in str(ui.query_one("#view-title", Static).render())
            assert backend._chrome is None
            assert ui.query_one("#command", Input).has_focus

    asyncio.run(exercise())
