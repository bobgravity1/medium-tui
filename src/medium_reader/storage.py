"""Local SQLite cache. Stores fetched articles and explicitly requested homepage cards."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .core import Article, ReaderError, article_key


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "reader.sqlite3")
        self.db.execute("CREATE TABLE IF NOT EXISTS articles (id TEXT PRIMARY KEY, data TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, data TEXT)")

    def close(self) -> None:
        self.db.close()

    def put(self, article: Article) -> None:
        previous = self.get(article.url)
        preview_sources = {"rss-preview", "home-preview"}
        if (
            previous
            and previous.markdown
            and (
                not article.markdown
                or (article.source in preview_sources and previous.source not in preview_sources)
            )
        ):
            return
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO articles VALUES (?, ?)",
                (article_key(article.url), json.dumps(article.to_dict())),
            )

    def get(self, url: str) -> Article | None:
        row = self.db.execute(
            "SELECT data FROM articles WHERE id = ?", (article_key(url),)
        ).fetchone()
        return Article(**json.loads(row[0])) if row else None

    def set_state(self, key: str, data: object) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, json.dumps(data)))

    def state(self, key: str) -> object:
        row = self.db.execute("SELECT data FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def remember_topic(self, topic: str, articles: list[Article]) -> None:
        for article in articles:
            self.put(article)
        self.set_state(f"topic:{topic}", [a.url for a in articles])

    def topic(self, topic: str) -> list[Article]:
        urls = self.state(f"topic:{topic}")
        if urls is None:
            raise ReaderError(f"No offline results for '{topic}'. Search it online first.")
        return [article for url in urls if (article := self.get(url)) is not None]

    def remember_home(self, articles: list[Article]) -> None:
        for article in articles:
            self.put(article)
        self.set_state("home_results", [article.url for article in articles])

    def home(self) -> list[Article]:
        urls = self.state("home_results")
        if urls is None:
            raise ReaderError("No offline homepage results. Run 'medium-reader home' online first.")
        return [article for url in urls if (article := self.get(url)) is not None]

    def remember_favorites(self, articles: list[Article]) -> None:
        for article in articles:
            self.put(article)
        self.set_state("favorite_results", [article.url for article in articles])

    def favorites(self) -> list[Article]:
        urls = self.state("favorite_results")
        if urls is None:
            raise ReaderError(
                "No offline saved stories. Run 'medium-reader favorites' online first."
            )
        return [article for url in urls if (article := self.get(url)) is not None]

    def remember_results(self, articles: list[Article]) -> None:
        self.set_state("last_results", [article.url for article in articles])

    def result(self, number: int) -> Article:
        urls = self.state("last_results") or []
        if number < 1 or number > len(urls):
            raise ReaderError(
                "No result at that number. Search a topic or browse home "
                "and use a displayed number."
            )
        article = self.get(urls[number - 1])
        if article is None:
            raise ReaderError(
                "That cached result is missing. Search the topic or browse home again."
            )
        return article
