"""CLI commands and a keyboard-driven interactive reading loop."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from contextlib import nullcontext
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from platformdirs import user_cache_path
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from . import __version__
from .chrome import ChromeFeed, ConnectionRequired
from .core import Article, MediumClient, ReaderError, clean_line, safe_url, topic_slug
from .home import merge_home
from .storage import Store


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use a whole number") from exc
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("use a number between 1 and 100")
    return number


def timeout_value(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use a number of seconds") from exc
    if not 0 < number <= 120:
        raise argparse.ArgumentTypeError("timeout must be greater than 0 and at most 120 seconds")
    return number


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="override the local cache directory",
    )
    common.add_argument(
        "--timeout",
        type=timeout_value,
        default=argparse.SUPPRESS,
        help="network timeout in seconds (default: 20)",
    )
    common.add_argument(
        "--no-color", action="store_true", default=argparse.SUPPRESS, help="disable ANSI colors"
    )
    common.add_argument(
        "--classic",
        action="store_true",
        default=argparse.SUPPRESS,
        help="use the original line-based interface",
    )
    p = argparse.ArgumentParser(
        prog="medium-reader",
        parents=[common],
        description="Search Medium topics and read public articles in your terminal.",
        epilog="Run without arguments for interactive mode. Topic search needs no account.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command")
    search = sub.add_parser(
        "search", aliases=["/search"], parents=[common], help="find recent stories by topic/tag"
    )
    search.add_argument("topic", nargs="+", help="a topic such as python or machine learning")
    search.add_argument(
        "--limit", type=positive_int, default=10, help="maximum results (default: 10)"
    )
    search.add_argument("--contains", default="", help="filter fetched titles and text by a phrase")
    search.add_argument(
        "--json", action="store_true", help="print machine-readable result metadata"
    )
    search.add_argument(
        "--pick", action="store_true", help="choose and read a result interactively"
    )
    search.add_argument("--offline", action="store_true", help="use the last fetched topic feed")
    search.add_argument("--sort", choices=("recent", "popular"), default="recent")
    home = sub.add_parser(
        "home", aliases=["/home"], parents=[common], help="browse your signed-in Medium homepage"
    )
    home.add_argument("--offline", action="store_true", help="browse previously fetched home cards")
    home.add_argument(
        "--limit", type=positive_int, default=20, help="cards to fetch for --list/--json"
    )
    home.add_argument("--page-size", type=positive_int, default=8, help="stories per browser page")
    home.add_argument("--json", action="store_true", help="print story metadata as JSON")
    home.add_argument("--list", action="store_true", help="print once instead of browsing")
    home.add_argument("--sort", choices=("recent", "popular"), default="recent")
    sub.add_parser(
        "login",
        aliases=["/login"],
        parents=[common],
        help="open your default browser and connect Medium",
    )
    sub.add_parser(
        "logout",
        aliases=["/logout"],
        parents=[common],
        help="disconnect the reader; keep your browser signed in",
    )
    favorites = sub.add_parser(
        "favorites", aliases=["/favorites"], parents=[common], help="browse your saved Reading list"
    )
    favorites.add_argument(
        "--offline", action="store_true", help="browse previously fetched saved stories"
    )
    favorites.add_argument("--page-size", type=positive_int, default=8)
    favorites.add_argument("--json", action="store_true")
    favorites.add_argument("--list", action="store_true")
    favorites.add_argument("--sort", choices=("recent", "popular"), default="recent")
    read = sub.add_parser(
        "read",
        aliases=["/read"],
        parents=[common],
        help="read a result number or Medium article URL",
    )
    read.add_argument("target", help="a number from your latest search, or an HTTPS Medium URL")
    read.add_argument("--format", choices=("rich", "plain", "markdown", "json"), default=None)
    read.add_argument("--pager", action="store_true", help="read in a pager (press q to return)")
    read.add_argument("--save", type=Path, help="also save a Markdown copy at this path")
    read.add_argument(
        "--force", action="store_true", help="allow --save to replace an existing file"
    )
    read.add_argument("--offline", action="store_true", help="read only from the local cache")
    read.add_argument("--refresh", action="store_true", help="fetch the public article page again")
    sub.add_parser("interactive", parents=[common], help="search, pick, read, and repeat")
    return p


def document(article: Article) -> str:
    return (
        f"# {article.title}\n\n"
        f"By {article.author}"
        + (f" · {article.published}" if article.published else "")
        + f"\n\nSource: {article.url}\n\n> {article.note}\n\n{article.markdown}\n"
    )


def show_results(
    console: Console,
    topic: str,
    articles: list[Article],
    offline: bool,
    *,
    start: int = 1,
    description: str = "Recent stories from the topic's public RSS feed",
) -> None:
    console.print(Text(f"Medium · {topic}", style="bold cyan"))
    console.print("Cached feed" if offline else description, style="dim")
    if not articles:
        console.print("No stories found. Try a broader topic or remove --contains.")
        return
    table = Table(box=None, padding=(0, 1), expand=True)
    table.add_column("#", style="cyan", width=3, no_wrap=True)
    table.add_column("Story", ratio=4)
    table.add_column("Author", ratio=1)
    table.add_column("Date", width=10)
    for number, article in enumerate(articles, start):
        title = Text(article.title, style="bold")
        title.append(f"\n{article.url}", style="dim")
        table.add_row(str(number), title, Text(article.author), Text(article.published))
    console.print(table)
    console.print(f"\nRead a result: medium-reader read {start}", style="dim")


def show_article(
    console: Console, article: Article, output_format: str, pager: bool = False
) -> None:
    if output_format == "json":
        print(json.dumps(article.to_dict(), ensure_ascii=False, indent=2))
        return
    if output_format == "markdown":
        print(document(article), end="")
        return
    context = console.pager(styles=False) if pager and console.is_terminal else nullcontext()
    with context:
        if output_format == "plain":
            plain = Console(
                file=console.file, width=console.width, color_system=None, force_terminal=False
            )
            plain.print(Markdown(document(article), hyperlinks=False))
        else:
            console.print()
            console.print(Text(article.title, style="bold cyan"))
            console.print(
                Text(
                    f"{article.author} · {article.published or 'Date unavailable'}"
                    f" · ~{article.minutes} min of available text",
                    style="dim",
                )
            )
            console.print(Text(article.url, style="dim"))
            console.print(Panel(Text(article.note), border_style="yellow", padding=(0, 1)))
            console.print(Markdown(article.markdown, hyperlinks=False))


class App:
    def __init__(self, client: MediumClient, store: Store, console: Console):
        self.client, self.store, self.console = client, store, console
        self._chrome: ChromeFeed | None = None

    @property
    def chrome(self) -> ChromeFeed:
        if self._chrome is None or self._chrome.closed:
            self._chrome = ChromeFeed()
        return self._chrome

    def close(self) -> None:
        if self._chrome is not None:
            self._chrome.close()

    def search(
        self, topic: str, limit: int = 10, contains: str = "", offline: bool = False
    ) -> list[Article]:
        topic = topic_slug(topic)
        if offline:
            articles = self.store.topic(topic)
        else:
            articles = self.client.search(topic)
            self.store.remember_topic(topic, articles)
        needle = contains.casefold()
        articles = [a for a in articles if needle in f"{a.title}\n{a.markdown}".casefold()][:limit]
        # Result numbers track the last displayed list, including an empty one.
        self.store.remember_results(articles)
        return articles

    def read(self, target: str, offline: bool = False, refresh: bool = False) -> Article:
        if offline and refresh:
            raise ReaderError("--offline and --refresh cannot be used together.")
        if target.isdecimal():
            cached = self.store.result(int(target))
            url = cached.url
        else:
            url = safe_url(target)
            cached = self.store.get(url)
        if cached and cached.markdown and not refresh:
            if offline or cached.source not in ("rss-preview", "home-preview"):
                return cached
        if offline:
            raise ReaderError("No cached article text. Read it online or search its topic first.")
        try:
            article = self.client.read(url)
        except ReaderError as exc:
            if cached and cached.markdown and not refresh:
                return replace(cached, note=f"Only the cached topic preview is available. {exc}")
            raise
        if cached and not article.tags:
            article.tags = cached.tags
        self.store.put(article)
        return article

    def choose(self, articles: list[Article]) -> None:
        if not articles:
            return
        while True:
            value = Prompt.ask(
                "\nRead which number? (b = back, q = quit)", console=self.console
            ).strip()
            if value.lower() == "q":
                raise EOFError
            if value.lower() == "b":
                return
            if not value.isdecimal() or not 1 <= int(value) <= len(articles):
                self.console.print("Enter one of the displayed numbers.", style="yellow")
                continue
            try:
                with self.console.status("Loading article…"):
                    article = self.read(value)
                show_article(self.console, article, "rich", pager=True)
            except ReaderError as exc:
                self.console.print(Text(str(exc), style="red"))

    def remember_home(self, articles: list[Article]) -> None:
        self.store.remember_home(articles)
        self.store.remember_results(articles)

    def browser_space(self, key: str = "home_browser_space") -> int | None:
        value = self.store.state(key)
        return value if isinstance(value, int) else None

    def home(self, limit: int = 20, offline: bool = False, sort: str = "recent") -> list[Article]:
        if offline:
            articles = self.store.home()
        else:
            with nullcontext(self.chrome) as feed:
                articles = []
                for _ in range(10):
                    expanded = merge_home(articles, feed.load_more())
                    if len(expanded) == len(articles):
                        break
                    articles = expanded
                    if len(articles) >= limit:
                        break
            self.store.remember_home(articles)
        articles = self._sort_articles(articles, sort)[:limit]
        self.store.remember_results(articles)
        return articles

    def login(self) -> None:
        feed = self.chrome
        feed.begin_login()
        self.console.print(
            "Connect the Medium Reader extension in Chrome, then sign in if needed. "
            "Your password and cookies stay in Chrome."
        )
        answer = (
            Prompt.ask("When Medium is ready, type done (q cancels)", console=self.console)
            .strip()
            .lower()
        )
        if answer != "done":
            self.console.print("Connection paused. Enter /login to open the browser again.")
            return
        self.remember_home(feed.confirm_login())
        self.console.print("Chrome connected. Use /home or /favorites.")

    def browse_home(self, offline: bool = False, page_size: int = 8, sort: str = "recent") -> None:
        context = nullcontext(None) if offline else nullcontext(self.chrome)
        with context as feed:
            if feed is not None:
                feed.loaded = False
            with self.console.status("Loading your homepage…"):
                articles = self.store.home() if offline else feed.load_more()
            self.remember_home(articles)
            articles = self._sort_articles(articles, sort)
            self.store.remember_results(articles)
            if not articles:
                self.console.print("No cached homepage stories. Run medium-reader home online.")
                return
            offset = 0
            while True:
                show_results(
                    self.console,
                    "Your homepage",
                    articles[offset : offset + page_size],
                    offline,
                    start=offset + 1,
                    description="Stories from your signed-in Medium homepage",
                )
                value = (
                    Prompt.ask(
                        "\nStory number, n = next, p = previous, /popular, /recent, b, q",
                        console=self.console,
                    )
                    .strip()
                    .lower()
                )
                if value == "q":
                    raise EOFError
                if value == "b":
                    return
                if value == "/favorites":
                    self.browse_favorites()
                    self.store.remember_results(articles)
                    continue
                if value == "/login":
                    self.login()
                    self.store.remember_results(articles)
                    continue
                if value == "/logout":
                    self.logout()
                    return
                if value == "p":
                    offset = max(0, offset - page_size)
                    continue
                if value in ("/popular", "/recent"):
                    try:
                        articles = self._sort_articles(articles, value[1:])
                        sort = value[1:]
                        self.store.remember_results(articles)
                        offset = 0
                        self.console.print(f"Sorted by {value[1:]}.", style="dim")
                    except ReaderError as exc:
                        self.console.print(Text(str(exc), style="yellow"))
                    continue
                if value == "n":
                    next_offset = offset + page_size
                    if next_offset >= len(articles) and feed is not None:
                        with self.console.status("Loading more homepage stories…"):
                            expanded = merge_home(articles, feed.load_more())
                        if len(expanded) > len(articles):
                            previous_size = len(articles)
                            articles = self._sort_articles(expanded, sort)
                            self.remember_home(articles)
                            if previous_size < next_offset:
                                # Show newly loaded cards that fill the current page first.
                                continue
                    if next_offset < len(articles):
                        offset = next_offset
                    else:
                        self.console.print("No more stories loaded.", style="dim")
                    continue
                if not value.isdecimal() or not offset < int(value) <= min(
                    len(articles), offset + page_size
                ):
                    self.console.print(
                        "Enter a number on this page, n, p, b, or q.", style="yellow"
                    )
                    continue
                try:
                    with self.console.status("Loading article…"):
                        article = self.read(value, offline=offline)
                    show_article(self.console, article, "rich", pager=True)
                except ReaderError as exc:
                    self.console.print(Text(str(exc), style="red"))

    def interactive(self) -> None:
        self.console.print(
            Panel(
                "[bold cyan]MEDIUM READER[/bold cyan]\n"
                "Search a topic. Pick a story. Read in your terminal.\n"
                "[dim]Examples: python · design · machine learning\n"
                "Commands: /home, /favorites, /login, /logout, /popular, /recent.\n"
                "In the pager, q returns to your results.[/dim]",
                border_style="cyan",
            )
        )
        while True:
            query = Prompt.ask("\nTopic", console=self.console).strip()
            if query.lower() in ("q", "quit", "exit"):
                return
            if not query:
                continue
            try:
                if query.lower() == "/home":
                    self.browse_home()
                    continue
                if query.lower() == "/login":
                    self.login()
                    continue
                if query.lower() == "/logout":
                    self.logout()
                    continue
                if query.lower() == "/favorites":
                    self.browse_favorites()
                    continue
                topic = topic_slug(query)
                with self.console.status("Fetching Medium's topic feed…"):
                    articles = self.search(topic)
                self.browse_results(topic, articles)
            except ReaderError as exc:
                self.console.print(Text(str(exc), style="red"))

    def _sort_articles(self, articles: list[Article], mode: str) -> list[Article]:
        if not articles:
            return []
        if mode == "popular":
            if not any(article.claps is not None for article in articles):
                raise ReaderError(
                    "Medium did not expose clap counts for these results, so they cannot be ranked "
                    "by popularity."
                )
            return sorted(articles, key=lambda item: item.claps or 0, reverse=True)

        def published(article: Article) -> date:
            raw = article.published.strip()
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                relative = re.fullmatch(r"(\d+)\s*(m|min|h|d|w|wk|mo|y|yr)\s*ago", raw.lower())
                if relative:
                    amount = int(relative.group(1))
                    unit = relative.group(2)
                    days = {
                        "m": 0,
                        "min": 0,
                        "h": 0,
                        "d": 1,
                        "w": 7,
                        "wk": 7,
                        "mo": 30,
                        "y": 365,
                        "yr": 365,
                    }[unit]
                    return date.today() - timedelta(days=amount * days)
                try:
                    parsed = datetime.strptime(f"{raw} {date.today().year}", "%b %d %Y").date()
                    return parsed
                except ValueError:
                    return date.min

        return sorted(articles, key=published, reverse=True)

    def browse_results(
        self, topic: str, articles: list[Article], page_size: int = 8, *, offline: bool = False
    ) -> None:
        offset = 0
        while articles:
            show_results(
                self.console,
                topic,
                articles[offset : offset + page_size],
                offline,
                start=offset + 1,
                description="Stories available from Medium's public topic feed",
            )
            value = (
                Prompt.ask(
                    "\nStory number, n = next, p = previous, /popular, /recent, "
                    "b = topic, q = quit",
                    console=self.console,
                )
                .strip()
                .lower()
            )
            if value in ("q", "quit"):
                raise EOFError
            if value in ("b", "back"):
                return
            if value == "/home":
                self.browse_home()
                return
            if value == "/favorites":
                self.browse_favorites()
                return
            if value == "/login":
                self.login()
                return
            if value == "/logout":
                self.logout()
                return
            if value == "n":
                if offset + page_size < len(articles):
                    offset += page_size
                else:
                    self.console.print(
                        "No more results are exposed by this topic feed.", style="dim"
                    )
                continue
            if value == "p":
                offset = max(0, offset - page_size)
                continue
            if value in ("/popular", "/recent"):
                try:
                    articles = self._sort_articles(articles, value[1:])
                    offset = 0
                    self.store.remember_results(articles)
                    self.console.print(f"Sorted by {value[1:]}.", style="dim")
                except ReaderError as exc:
                    self.console.print(Text(str(exc), style="yellow"))
                continue
            if not value.isdecimal() or not offset < int(value) <= min(
                len(articles), offset + page_size
            ):
                self.console.print(
                    "Enter a number on this page, n, p, /popular, /recent, b, or q.", style="yellow"
                )
                continue
            try:
                with self.console.status("Loading article…"):
                    article = self.read(value, offline=offline)
                show_article(self.console, article, "rich", pager=True)
            except ReaderError as exc:
                self.console.print(Text(str(exc), style="red"))

    def favorites(self, offline: bool = False, sort: str = "recent") -> list[Article]:
        if offline:
            articles = self.store.favorites()
        else:
            with nullcontext(self.chrome) as feed:
                with self.console.status("Loading your saved stories…"):
                    articles = feed.load_favorites()
                self.store.remember_favorites(articles)
        articles = self._sort_articles(articles, sort)
        self.store.remember_results(articles)
        return articles

    def browse_favorites(
        self, offline: bool = False, page_size: int = 8, sort: str = "recent"
    ) -> list[Article]:
        articles = self.favorites(offline, sort)
        if not articles:
            self.console.print("Your Medium Reading list is empty.", style="dim")
            return articles
        offset = 0
        while True:
            show_results(
                self.console,
                "Your saved stories",
                articles[offset : offset + page_size],
                offline,
                start=offset + 1,
                description="Stories saved in your Medium Reading list",
            )
            answer = (
                Prompt.ask(
                    "\nStory number, n = next, p = previous, /popular, /recent, b, q",
                    console=self.console,
                )
                .strip()
                .lower()
            )
            if answer in ("q", "quit"):
                raise EOFError
            if answer in ("b", "back"):
                return articles
            if answer == "/logout":
                self.logout()
                return articles
            if answer == "/home":
                self.browse_home()
                return articles
            if answer == "/login":
                self.login()
                return articles
            if answer == "n":
                offset = min(offset + page_size, max(0, len(articles) - 1))
                continue
            if answer == "p":
                offset = max(0, offset - page_size)
                continue
            if answer in ("/popular", "/recent"):
                try:
                    articles = self._sort_articles(articles, answer[1:])
                    sort = answer[1:]
                    self.store.remember_results(articles)
                    offset = 0
                except ReaderError as exc:
                    self.console.print(Text(str(exc), style="yellow"))
                continue
            if answer.isdecimal() and offset < int(answer) <= min(
                len(articles), offset + page_size
            ):
                article = self.read(answer, offline=offline)
                show_article(self.console, article, "rich", pager=True)

    def logout(self) -> None:
        self.close()
        self.store.set_state("home_results", [])
        self.store.set_state("favorite_results", [])
        self.store.set_state("last_results", [])
        self.console.print("Reader disconnected. You remain signed in to Medium in Chrome.")


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    if args.command is not None:
        args.command = args.command.lstrip("/")
    if args.command is None:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            p.print_help()
            return 0
        args.command = "interactive"
    if args.command in ("interactive", "login") or getattr(args, "pick", False):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            p.error("interactive mode requires a terminal; use search or read without --pick")
    if getattr(args, "pick", False) and args.json:
        p.error("--pick and --json cannot be used together")
    if getattr(args, "force", False) and args.save is None:
        p.error("--force requires --save")
    if getattr(args, "offline", False) and getattr(args, "refresh", False):
        p.error("--offline and --refresh cannot be used together")
    no_color = getattr(args, "no_color", False) or "NO_COLOR" in os.environ
    console = Console(no_color=no_color, highlight=False)
    errors = Console(stderr=True, no_color=no_color, highlight=False)
    directory = getattr(args, "data_dir", None)
    directory = directory or Path(
        os.environ.get("MEDIUM_READER_HOME", "") or user_cache_path("medium-reader")
    )
    client = None
    store = None
    app = None
    try:
        store = Store(directory.expanduser())
        client = MediumClient(timeout=getattr(args, "timeout", 20))
        app = App(client, store, console)
        visual = console.is_terminal and sys.stdin.isatty() and not getattr(args, "classic", False)
        if visual and (
            args.command in ("interactive", "login")
            or (args.command in ("home", "favorites") and not args.json and not args.list)
            or (args.command == "search" and args.pick)
        ):
            from .tui import ReaderUI

            ReaderUI(app, args=args).run()
            return 0
        if args.command in ("home", "favorites") and not args.offline:
            feed = app.chrome
            if not feed.connected:
                if not sys.stdin.isatty():
                    raise ConnectionRequired(
                        "Live home/favorites need a Chrome connection in a terminal. "
                        "Run medium-reader, enter /login, or use --offline for cached results."
                    )
                errors.print(
                    "Opening your browser. Click Connect and sign in to Medium if prompted."
                )
                feed.begin_login()
                feed.wait_for_login()
        if args.command == "interactive":
            app.interactive()
        elif args.command == "login":
            app.login()
        elif args.command == "logout":
            app.logout()
        elif args.command == "home":
            if console.is_terminal and sys.stdin.isatty() and not args.json and not args.list:
                app.browse_home(args.offline, args.page_size, args.sort)
            else:
                articles = app.home(args.limit, args.offline, args.sort)
                if args.json:
                    print(
                        json.dumps(
                            [
                                {
                                    "number": number,
                                    **{
                                        k: v
                                        for k, v in article.to_dict().items()
                                        if k != "markdown"
                                    },
                                }
                                for number, article in enumerate(articles, 1)
                            ],
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                else:
                    show_results(
                        console,
                        "Your homepage",
                        articles,
                        args.offline,
                        description="Stories from your signed-in Medium homepage",
                    )
        elif args.command == "favorites":
            if console.is_terminal and sys.stdin.isatty() and not args.json and not args.list:
                app.browse_favorites(args.offline, args.page_size, args.sort)
            else:
                articles = app.favorites(args.offline, args.sort)
                if args.json:
                    print(
                        json.dumps(
                            [
                                {
                                    "number": number,
                                    **{
                                        key: value
                                        for key, value in article.to_dict().items()
                                        if key != "markdown"
                                    },
                                }
                                for number, article in enumerate(articles, 1)
                            ],
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                else:
                    show_results(
                        console,
                        "Your saved stories",
                        articles,
                        args.offline,
                        description="Stories saved in your Medium Reading list",
                    )
        elif args.command == "search":
            topic = topic_slug(" ".join(args.topic))
            articles = app.search(topic, args.limit, args.contains, args.offline)
            articles = app._sort_articles(articles, args.sort)
            app.store.remember_results(articles)
            if args.json:
                result = []
                for number, article in enumerate(articles, 1):
                    data = article.to_dict()
                    data.pop("markdown")
                    result.append({"number": number, **data})
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                show_results(console, topic, articles, args.offline)
            if args.pick:
                app.browse_results(topic, articles, offline=args.offline)
        else:
            article = app.read(args.target, args.offline, args.refresh)
            if args.save:
                destination = args.save.expanduser()
                try:
                    with destination.open("w" if args.force else "x", encoding="utf-8") as handle:
                        handle.write(document(article))
                except FileExistsError as exc:
                    raise ReaderError(
                        "That file already exists. Use --force to replace it."
                    ) from exc
                errors.print(Text(f"Saved Markdown to {destination}"))
            output_format = args.format or ("rich" if console.is_terminal else "markdown")
            if visual and output_format == "rich" and (args.format is None or args.pager):
                from .tui import ReaderUI

                ReaderUI(app, args=args, article=article).run()
            else:
                show_article(console, article, output_format, args.pager)
        return 0
    except (KeyboardInterrupt, EOFError):
        return 130 if sys.exc_info()[0] is KeyboardInterrupt else 0
    except BrokenPipeError:
        # A consumer such as `head` may close stdout before the article is finished.
        sys.stdout = open(os.devnull, "w")
        return 0
    except (ReaderError, OSError, sqlite3.Error) as exc:
        errors.print(Text(f"Error: {clean_line(str(exc))}", style="red"))
        return 1
    finally:
        if app:
            app.close()
        if client:
            client.close()
        if store:
            store.close()
