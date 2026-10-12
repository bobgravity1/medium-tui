"""Two-pane feed browser and synchronized Markdown reader.

Blocking network/browser work runs off the UI loop; SQLite stays on the UI thread.
"""

from __future__ import annotations

import asyncio
import os
import re
from argparse import Namespace
from dataclasses import replace
from typing import TYPE_CHECKING

from rich.text import Text
from textual import events, on, work
from textual.app import App as TextualApp
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Footer, Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from .chrome import ConnectionRequired, SignInRequired
from .clipboard import copy_text
from .core import Article, ReaderError, article_key, topic_slug
from .home import merge_home

if TYPE_CHECKING:
    from .cli import App


def meter(fraction: float, width: int = 8) -> str:
    filled = round(max(0, min(1, fraction)) * width)
    return "━" * filled + "─" * (width - filled)


def section_progress(position: float, starts: list[float], end: float) -> list[float]:
    """Progress uses rendered heading positions, so wrapping and resizing are respected."""
    return [
        max(0.0, min(1.0, (position - start) / max(1, stop - start)))
        for start, stop in zip(starts, starts[1:] + [end])
    ]


def article_body(article: Article) -> str:
    # Page/RSS parsers sometimes include the title already. Render it only once.
    body = article.markdown.strip()
    lines = body.splitlines()
    if lines and re.sub(r"^#{1,6}\s+", "", lines[0]).strip() == article.title.strip():
        body = "\n".join(lines[1:]).lstrip()
    return body or "No article text is available in this preview."


class ReaderUI(TextualApp):
    TITLE = "Medium Reader"
    CSS_PATH = "reader.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "back", "Back"),
        Binding("slash", "command", "/ Search / commands"),
        Binding("tab", "focus_next", "Switch pane", show=False),
        Binding("j", "move(1)", "Down", show=False),
        Binding("k", "move(-1)", "Up", show=False),
        Binding("n", "more", "More / page down"),
        Binding("w", "sidebar", "Sidebar", show=False),
        Binding("g", "edge(False)", "Top", show=False),
        Binding("G", "edge(True)", "End", show=False),
    ]

    def __init__(
        self, backend: App, *, args: Namespace | None = None, article: Article | None = None
    ):
        original_no_color = os.environ.get("NO_COLOR")
        if getattr(args, "no_color", False):
            os.environ["NO_COLOR"] = "1"
        try:
            super().__init__()
        finally:
            if original_no_color is None:
                os.environ.pop("NO_COLOR", None)
            else:
                os.environ["NO_COLOR"] = original_no_color
        self.backend = backend
        self.args = args or Namespace(command="interactive")
        self.initial_article = article
        self.articles: list[Article] = []
        self.article: Article | None = None
        self.feed = None
        self.login_pending = False
        self.login_target = "home"
        self.showing_login_gate = False
        self.last_connection_state = ""
        self.source = "home"
        self.topic = ""
        self.sort = getattr(self.args, "sort", "recent")
        self.offline = getattr(self.args, "offline", False)
        self.busy = False
        self.reading = False
        self.heading_rows: list[tuple[int, str, str]] = []
        self.last_progress: tuple = ()
        self.sidebar_hidden = False
        self.saved_fraction = 0.0
        self.restore_pending = False
        self.edge_intent: bool | None = None
        self.active_section = 0
        self.copied_paragraph: Static | None = None
        self.copy_timer = None

    def compose(self) -> ComposeResult:
        yield Static("MEDIUM  /  reader", id="brand")
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                yield Static("LIBRARY", id="side-title")
                yield Static("Your daily reading", id="side-subtitle")
                yield OptionList(id="navigation")
                yield OptionList(id="outline")
                yield Static("", id="reading-progress")
            with Vertical(id="main"):
                yield Static("For you", id="view-title")
                yield Static("Recommended stories from your Medium homepage", id="view-meta")
                yield OptionList(id="stories")
                yield Static("", id="empty", markup=False)
                with VerticalScroll(id="reader"):
                    yield Markdown(id="document", open_links=False)
        with Horizontal(id="status-row"):
            yield Static("Ready", id="status", markup=False)
            yield Static("", id="copy-status", markup=False)
        yield Input(placeholder="Search a topic, or /home  /favorites  /login  /help", id="command")
        yield Footer()

    async def on_mount(self) -> None:
        self.theme = "textual-dark"
        self.query_one("#outline").display = False
        self.query_one("#reader").display = False
        self.query_one("#empty").display = False
        self.build_navigation()
        self.set_interval(0.15, self.sync_progress)
        self.set_interval(0.5, self.check_connection)
        if self.initial_article:
            await self.display_article(self.initial_article)
        elif self.args.command == "search":
            self.operate("search", " ".join(self.args.topic))
        elif self.args.command == "favorites":
            self.login_target = "favorites"
            self.operate("favorites" if self.offline else "login")
        elif self.args.command == "login":
            self.operate("login")
        elif self.offline or self.args.command == "home":
            self.operate("home" if self.offline else "login")
        else:
            self.show_login_gate()
        self.on_resize()

    def build_navigation(self) -> None:
        nav = self.query_one("#navigation", OptionList)
        nav.clear_options()
        nav.add_options(
            [
                Option(Text("For you", style="bold"), id="home"),
                Option("Saved stories", id="favorites"),
                Option("Search topics", id="search"),
                None,
            ]
        )
        for topic in (self.backend.store.state("ui_topics") or [])[:8]:
            nav.add_option(Option(Text(f"# {topic}", style="#a6adc8"), id=f"topic:{topic}"))
        nav.add_options([None, Option("Connect Chrome / login", id="login")])

    def show_login_gate(self, message: str = "") -> None:
        self.save_position()
        self.clear_copy_feedback()
        self.reading = False
        self.restore_pending = False
        self.showing_login_gate = True
        self.query_one("#reader").display = False
        self.query_one("#outline").display = False
        self.query_one("#navigation").display = True
        self.query_one("#side-title", Static).update("LIBRARY")
        self.query_one("#side-subtitle", Static).update("Chrome connection")
        self.query_one("#reading-progress", Static).update("")
        self.show_empty(
            "Connect your Medium account",
            message
            or "Enter /login to open your default browser and connect Chrome.\n\n"
            "Already signed in to Medium? The reader will use that Chrome session.\n"
            "The Medium Reader extension shares feed cards; your login stays in Chrome.\n\n"
            "You can also search a topic below without connecting an account.",
        )
        self.query_one("#command", Input).focus()

    def check_connection(self) -> None:
        feed = self.backend._chrome
        if self.login_pending and feed and feed.connected and not self.busy:
            if self.showing_login_gate:
                self.operate("done")
            else:
                # Chrome may finish sign-in while the user reads/searches. Do not
                # unexpectedly replace their article or result-number mapping.
                self.login_pending = False
                self.status("Chrome connected · /home or /favorites when you're ready")
        elif self.login_pending and feed and not self.busy:
            state = feed.state
            if state != self.last_connection_state and isinstance(state, str):
                self.last_connection_state = state
                messages = {
                    "waiting-for-extension": "Waiting for the extension's Connect button…",
                    "waiting-for-browser": "Chrome connected; waiting for Medium to load…",
                    "sign-in-required": (
                        "Sign in to Medium in Chrome. This reader updates automatically."
                    ),
                    "disconnected": "Chrome connection lost. Enter /login to reopen the browser.",
                    "error": (
                        "Chrome could not load the account view. "
                        "Refresh its Medium tab or retry /login."
                    ),
                }
                if state in messages:
                    self.status(messages[state])

    def status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def show_empty(self, title: str, message: str) -> None:
        self.query_one("#view-title", Static).update(Text(title))
        self.query_one("#view-meta", Static).update("")
        self.query_one("#stories").display = False
        empty = self.query_one("#empty", Static)
        empty.update(message)
        empty.display = True

    def show_feed(self, *, reset: bool = False) -> None:
        self.showing_login_gate = False
        self.reading = False
        self.heading_rows = []
        self.query_one("#outline").display = False
        self.query_one("#navigation").display = True
        self.query_one("#reader").display = False
        self.query_one("#side-title", Static).update("LIBRARY")
        self.query_one("#side-subtitle", Static).update("Your daily reading")
        self.query_one("#reading-progress", Static).update("")
        titles = {"home": "For you", "favorites": "Saved stories", "search": f"# {self.topic}"}
        title = titles[self.source]
        self.query_one("#view-title", Static).update(Text(title))
        meta = f"{len(self.articles)} stories  ·  {self.sort}"
        if self.offline:
            meta += "  ·  cached"
        if self.source == "search":
            meta += "  ·  RSS batch"
        self.query_one("#view-meta", Static).update(Text(meta))
        stories = self.query_one("#stories", OptionList)
        index = stories.highlighted
        stories.clear_options()
        for i, article in enumerate(self.articles):
            row = Text(f"{i + 1:02}  ", style="#7f849c")
            row.append(article.title, style="bold #cdd6f4")
            row.append(
                f"\n    {article.author}  ·  {article.published or 'Date unavailable'}",
                style="#a6adc8",
            )
            if article.claps is not None:
                row.append(f"  ·  {article.claps:,} claps", style="#a6e3a1")
            row.append("\n")
            stories.add_option(Option(row, id=f"story:{i}"))
        stories.display = bool(self.articles)
        self.query_one("#empty").display = not self.articles
        if not self.articles:
            self.query_one("#empty", Static).update(
                "Your Reading list is empty. Save a story on Medium and refresh with /favorites."
                if self.source == "favorites"
                else "No stories found. Try another topic or /home."
            )
        elif reset or index is None:
            stories.highlighted = 0
        else:
            stories.highlighted = min(index, len(self.articles) - 1)
        self.backend.store.remember_results(self.articles)
        (stories if self.articles else self.query_one("#command", Input)).focus()
        self.on_resize()

    @work()
    async def operate(self, action: str, value: str = "") -> None:
        if self.busy:
            self.status("Still loading. You can scroll or quit while this finishes.")
            return
        self.busy = True
        self.status(f"Loading {action}…")
        try:
            await self.perform(action, value)
        except (ConnectionRequired, SignInRequired) as exc:
            if action in ("home", "favorites"):
                self.login_target = action
            self.show_login_gate(str(exc) + " Enter /login to open the browser.")
            self.status(str(exc))
        except (ReaderError, OSError) as exc:
            if action in ("home", "favorites", "login", "done") and not self.articles:
                self.show_login_gate(str(exc) + "\n\nEnter /login to connect Chrome.")
            self.status(str(exc))
            self.notify(str(exc), title="Couldn't load", severity="warning", timeout=8)
        finally:
            if action == "done":
                self.login_pending = False
            self.busy = False

    async def perform(self, action: str, value: str) -> None:
        store = self.backend.store
        if action == "read":
            item = self.articles[int(value)]
            cached = store.get(item.url)
            if (
                cached
                and cached.markdown
                and (self.offline or cached.source not in ("rss-preview", "home-preview"))
            ):
                article = cached
            elif self.offline:
                raise ReaderError("No cached article text. Open this story online first.")
            else:
                try:
                    article = await asyncio.to_thread(self.backend.client.read, item.url)
                except ReaderError as exc:
                    if not cached or not cached.markdown:
                        raise
                    article = replace(
                        cached, note=f"Only the cached topic preview is available. {exc}"
                    )
                if cached and not article.tags:
                    article.tags = cached.tags
                store.put(article)
            await self.display_article(article)
            return
        if action == "login":
            if self.offline:
                raise ReaderError("Restart without --offline to sign in.")
            self.feed = self.backend.chrome
            await asyncio.to_thread(self.feed.begin_login)
            self.login_pending = True
            self.show_login_gate(
                "Waiting for Chrome…\n\nClick Connect on the page that opened. "
                "Sign in to Medium if prompted.\nThe feed appears here automatically.\n\n"
                "No Connect button? Follow the one-time extension setup on that page. "
                "On macOS, use Command–Shift–G in the folder picker to paste its folder path. "
                "Then reload the connection page in Chrome.\n\n"
                "If it opened in another browser, copy its full address into your normal "
                "Chrome window. Keep the connected Medium tab open."
            )
            self.status("Waiting for Chrome · /login opens the browser again · topics still work")
            return
        if action == "done":
            if self.offline:
                raise ReaderError("Restart without --offline to connect Chrome.")
            self.feed = self.backend.chrome
            articles = await asyncio.to_thread(self.feed.confirm_login)
            self.save_position()
            self.login_pending = False
            store.remember_home(articles)
            if self.login_target == "favorites":
                articles = await asyncio.to_thread(self.feed.load_favorites)
                store.remember_favorites(articles)
            self.source, self.articles = (
                self.login_target,
                self.backend._sort_articles(articles, "recent"),
            )
            self.sort = "recent"
            self.show_feed(reset=True)
            self.status("Chrome connected · /home · /favorites · /logout disconnects the reader")
            return
        if action == "logout":
            await asyncio.to_thread(self.backend.close)
            for key in ("home_results", "favorite_results", "last_results"):
                store.set_state(key, [])
            self.login_pending = False
            self.feed, self.articles = None, []
            self.show_login_gate("Reader disconnected. Enter /login to reconnect Chrome.")
            self.status("Reader disconnected. Medium stays signed in in Chrome.")
            return
        if action == "more":
            if self.reading:
                self.query_one("#reader", VerticalScroll).scroll_page_down()
                return
            if self.source != "home" or self.offline:
                self.status(
                    "All fetched stories are in this list. Topic RSS exposes a recent "
                    "batch; saved lists show the stories supplied by Medium."
                )
                return
            if self.feed is None:
                raise ReaderError("Use /home to reconnect the feed first.")
            incoming = await asyncio.to_thread(self.feed.load_more)
            previous = len(self.articles)
            self.articles = self.backend._sort_articles(
                merge_home(self.articles, incoming), self.sort
            )
            store.remember_home(self.articles)
            self.show_feed()
            self.status(
                f"{len(self.articles) - previous} more stories loaded. "
                f"{len(self.articles)} in this feed."
            )
            return
        if action in ("popular", "recent"):
            self.save_position()
            articles = self.backend._sort_articles(self.articles, action)
            self.sort, self.articles = action, articles
            self.show_feed(reset=True)
            self.status(f"Sorted loaded stories by {action}.")
            return
        if action in ("home", "favorites", "search"):
            self.save_position()
            if action == "search":
                topic = topic_slug(value)
                articles = (
                    store.topic(topic)
                    if self.offline
                    else await asyncio.to_thread(self.backend.client.search, topic)
                )
                if not self.offline:
                    store.remember_topic(topic, articles)
                contains = (
                    getattr(self.args, "contains", "").casefold()
                    if self.args.command == "search"
                    else ""
                )
                articles = [
                    a for a in articles if contains in f"{a.title}\n{a.markdown}".casefold()
                ]
                articles = articles[: getattr(self.args, "limit", 100)]
                topics = [topic] + [t for t in (store.state("ui_topics") or []) if t != topic]
                store.set_state("ui_topics", topics[:8])
                self.topic = topic
                self.build_navigation()
            elif self.offline:
                articles = store.home() if action == "home" else store.favorites()
            else:
                self.feed = self.backend.chrome
                if action == "home":
                    self.feed.loaded = False
                loader = self.feed.load_more if action == "home" else self.feed.load_favorites
                articles = await asyncio.to_thread(loader)
                if action == "home":
                    store.remember_home(articles)
                else:
                    store.remember_favorites(articles)
            if action in ("home", "favorites"):
                self.login_target = action
            self.source = action
            self.articles = articles
            fallback = ""
            try:
                self.articles = self.backend._sort_articles(articles, self.sort)
            except ReaderError:
                self.sort = "recent"
                self.articles = self.backend._sort_articles(articles, "recent")
                fallback = "Clap counts unavailable; showing recent stories."
            self.show_feed(reset=True)
            self.status(
                fallback
                or "Enter opens a story · ↑/↓ browse · n loads more homepage stories · /help"
            )

    async def display_article(self, article: Article) -> None:
        self.showing_login_gate = False
        self.clear_copy_feedback()
        self.article, self.reading = article, True
        self.edge_intent = None
        self.heading_rows = []
        self.last_progress = ()
        self.query_one("#navigation").display = False
        self.query_one("#outline").display = True
        self.query_one("#stories").display = False
        self.query_one("#empty").display = False
        self.query_one("#reader").display = True
        self.query_one("#side-title", Static).update("IN THIS ARTICLE")
        self.query_one("#side-subtitle", Static).update(f"~{article.minutes} min available text")
        self.query_one("#view-title", Static).update("READING")
        self.query_one("#view-meta", Static).update(
            Text(f"{article.author}  ·  {article.published or 'Date unavailable'}")
        )
        doc = self.query_one("#document", Markdown)
        await doc.update(f"# {article.title}\n\n{article_body(article)}")
        reader = self.query_one("#reader", VerticalScroll)
        self.saved_fraction = float(
            self.backend.store.state(f"position:{article_key(article.url)}") or 0
        )
        self.restore_pending = True
        self.call_after_refresh(self.restore_position)
        reader.focus()
        self.status(article.note)
        self.on_resize()

    def restore_position(self) -> None:
        if self.edge_intent is not None:
            self.sync_progress()
            return
        if not self.restore_pending or not self.reading:
            return
        self.restore_pending = False
        reader = self.query_one("#reader", VerticalScroll)
        reader.scroll_to(y=reader.max_scroll_y * self.saved_fraction, animate=False, force=True)
        self.sync_progress()

    @on(Markdown.TableOfContentsUpdated)
    def outline_updated(self, event: Markdown.TableOfContentsUpdated) -> None:
        # A queued TOC message can refer to blocks already replaced by a later
        # update. Always use the heading widgets currently mounted in this doc.
        self.refresh_outline()
        self.call_after_refresh(self.sync_progress)

    def refresh_outline(self) -> list[Static]:
        doc = self.query_one("#document", Markdown)
        headings = list(
            doc.query("MarkdownH1, MarkdownH2, MarkdownH3, MarkdownH4, MarkdownH5, MarkdownH6")
        )
        rows = [
            (
                int(type(heading).__name__[-1]),
                heading.renderable.plain
                if isinstance(heading.renderable, Text)
                else str(heading.renderable),
                heading.id,
            )
            for heading in headings
            if heading.id
        ]
        if rows == self.heading_rows:
            return headings
        self.heading_rows = rows
        outline = self.query_one("#outline", OptionList)
        index = outline.highlighted
        outline.clear_options()
        outline.add_options([Option(Text(title), id=key) for _, title, key in self.heading_rows])
        if index is not None and rows:
            outline.highlighted = min(index, len(rows) - 1)
        self.last_progress = ()
        return headings

    def sync_progress(self) -> None:
        if not self.reading:
            return
        reader = self.query_one("#reader", VerticalScroll)
        headings = self.refresh_outline()
        if self.edge_intent is not None and not self.busy:
            end = self.edge_intent
            self.edge_intent = None
            self.restore_pending = False
            # Textual otherwise defers even a nonanimated scroll until refresh,
            # leaving the progress calculation below at the previous position.
            scroll_edge = reader.scroll_end if end else reader.scroll_home
            scroll_edge(animate=False, immediate=True)
        position = reader.scroll_y
        fraction = position / reader.max_scroll_y if reader.max_scroll_y else 1.0
        if not headings:
            self.last_progress = (round(fraction * 100), 0, ())
            return
        # Virtual coordinates stay stable while a scroll event awaits repaint.
        starts = []
        for heading in headings:
            offset = heading.virtual_region.y
            parent = heading.parent
            while parent is not None and parent is not reader:
                offset += parent.virtual_region.y
                parent = parent.parent
            starts.append(max(0, offset))
        values = []
        for i, (level, _, _) in enumerate(self.heading_rows):
            stop = next(
                (starts[j] for j in range(i + 1, len(starts)) if self.heading_rows[j][0] <= level),
                reader.virtual_size.height,
            )
            values.append(section_progress(position, [starts[i]], stop)[0])
        values[0] = fraction
        if fraction >= 0.999:
            values = [1.0] * len(starts)
        current = max((i for i, start in enumerate(starts) if start <= position + 1), default=0)
        signature = (round(fraction * 100), current, tuple(round(v * 100) for v in values))
        if signature == self.last_progress:
            return
        self.last_progress, self.active_section = signature, current
        outline = self.query_one("#outline", OptionList)
        base_level = min(level for level, _, _ in self.heading_rows)
        for i, ((level, title, _), value) in enumerate(zip(self.heading_rows, values)):
            label = Text("  " * min(3, level - base_level))
            label.append("▸ " if i == current else "  ", style="#b4befe")
            label.append(title, style="bold #b4befe" if i == current else "#a6adc8")
            label.append(
                f"\n  {meter(value, 6)} {round(value * 100):3}%",
                style="#a6e3a1" if value >= 1 else "#7f849c",
            )
            outline.replace_option_prompt_at_index(i, label)
        if not outline.has_focus:
            outline.highlighted = current
        self.query_one("#reading-progress", Static).update(
            Text(
                f"{meter(fraction, 16)}  {round(fraction * 100)}%\n"
                f"Section {current + 1} of {len(starts)}",
                style="#b4befe",
            )
        )

    def save_position(self) -> None:
        if self.reading and self.article:
            reader = self.query_one("#reader", VerticalScroll)
            fraction = reader.scroll_y / reader.max_scroll_y if reader.max_scroll_y else 1.0
            self.backend.store.set_state(f"position:{article_key(self.article.url)}", fraction)

    @on(OptionList.OptionSelected)
    def selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "stories":
            self.operate("read", str(event.option_index))
        elif event.option_list.id == "outline":
            self.restore_pending = False
            self.edge_intent = None
            try:
                heading = self.query_one(f"#{event.option.id}")
            except NoMatches:
                self.call_after_refresh(self.sync_progress)
                return
            self.query_one("#reader", VerticalScroll).scroll_to_widget(
                heading, top=True, animate=False, force=True
            )
            self.query_one("#reader").focus()
            # Keep the outline marker in lockstep with an explicit outline jump,
            # rather than waiting for the periodic scroll-progress refresh.
            self.sync_progress()
        elif event.option_list.id == "navigation":
            key = event.option.id or ""
            if key == "search":
                self.query_one("#command", Input).focus()
            elif key.startswith("topic:"):
                self.operate("search", key[6:])
            else:
                self.operate(key)

    @on(Input.Submitted, "#command")
    def submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        event.input.value = ""
        if value.startswith("/"):
            command, _, argument = value[1:].partition(" ")
            command = command.lower()
            if command in ("quit", "q"):
                self.action_quit()
            elif command == "back":
                self.action_back()
            elif command == "help":
                self.status(
                    "/home /favorites /search TOPIC /recent /popular /login /done /logout "
                    "/back /quit · Tab panes · Enter select · w sidebar · PgUp/PgDn scroll"
                )
            elif command == "search" and not argument:
                self.status("Type a topic, for example: /search machine learning")
            elif command in (
                "home",
                "favorites",
                "search",
                "recent",
                "popular",
                "login",
                "done",
                "logout",
            ):
                self.operate(command, argument)
            else:
                self.status("Unknown command. Use /help, or type a topic without a slash.")
        else:
            self.operate("search", value)
        if self.reading:
            self.query_one("#reader").focus()
        elif self.query_one("#stories").display:
            self.query_one("#stories").focus()
        else:
            self.query_one("#command", Input).focus()

    @on(Markdown.LinkClicked)
    def link_clicked(self, event: Markdown.LinkClicked) -> None:
        event.prevent_default()
        if event.href.startswith("#"):
            self.query_one("#document", Markdown).goto_anchor(event.href[1:])
        else:
            self.status(f"Link: {event.href}")

    def action_command(self) -> None:
        command = self.query_one("#command", Input)
        command.value = "/"
        command.focus()
        command.cursor_position = 1

    def action_back(self) -> None:
        if self.query_one("#command", Input).has_focus:
            self.query_one("#command", Input).value = ""
            if self.showing_login_gate and self.articles:
                self.show_feed()
            else:
                self.query_one(
                    "#reader"
                    if self.reading
                    else "#stories"
                    if self.query_one("#stories").display
                    else "#navigation"
                ).focus()
        elif self.reading:
            self.save_position()
            self.show_feed()
            self.status("Back to your stories.")
        else:
            self.query_one("#navigation").focus()

    def action_more(self) -> None:
        self.operate("more")

    def action_move(self, direction: int) -> None:
        self.edge_intent = None
        self.restore_pending = False
        focused = self.focused
        if isinstance(focused, OptionList):
            focused.action_cursor_down() if direction > 0 else focused.action_cursor_up()
        elif self.reading:
            reader = self.query_one("#reader", VerticalScroll)
            reader.scroll_relative(y=direction * 3, animate=False)

    def action_edge(self, end: bool) -> None:
        self.restore_pending = False
        if isinstance(self.focused, OptionList):
            self.focused.action_last() if end else self.focused.action_first()
        elif self.reading:
            self.edge_intent = end
            self.sync_progress()
            self.call_after_refresh(self.sync_progress)

    def action_sidebar(self) -> None:
        self.sidebar_hidden = not self.sidebar_hidden
        self.on_resize()

    def on_resize(self) -> None:
        if self.is_mounted:
            self.query_one("#sidebar").display = not self.sidebar_hidden and self.size.width >= 72
            self.last_progress = ()

    def action_quit(self) -> None:
        self.save_position()
        self.exit()

    @on(events.Click, "MarkdownParagraph")
    async def copy_paragraph(self, event: events.Click) -> None:
        paragraph = event.widget
        if not self.reading or event.button != 1 or event.shift:
            return
        if not isinstance(paragraph, Static):
            return
        content = paragraph.renderable
        if not isinstance(content, Text) or not content.plain.strip():
            return
        event.stop()
        text = content.plain.strip()
        native = await asyncio.to_thread(copy_text, text)
        if not self.is_running or not self.reading or not paragraph.is_mounted:
            return
        if not native:
            # OSC 52 provides clipboard access through supporting terminals/SSH.
            self.copy_to_clipboard(text)
        self.clear_copy_feedback()
        paragraph.add_class("copied-paragraph")
        self.copied_paragraph = paragraph
        self.query_one("#copy-status", Static).update("Copied" if native else "Copy sent")
        self.copy_timer = self.set_timer(1.5, self.clear_copy_feedback)

    def clear_copy_feedback(self) -> None:
        if self.copy_timer:
            self.copy_timer.stop()
            self.copy_timer = None
        if self.copied_paragraph:
            self.copied_paragraph.remove_class("copied-paragraph")
            self.copied_paragraph = None
        if self.is_mounted:
            self.query_one("#copy-status", Static).update("")
