"""Fetch and parse public Medium content without a browser or private API."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from markdownify import markdownify

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
FEED_NOTE = (
    "Content supplied by Medium's public RSS feed; it may be an excerpt. "
    "Member-only stories are not provided in full through RSS."
)
PAGE_NOTE = "Public article text. Sign-in or member-only content may be omitted."
PREVIEW_NOTE = "Member-only story: this is the public preview available without signing in."


class ReaderError(Exception):
    """An actionable error safe to show without a traceback."""


def clean_text(value: str) -> str:
    """Keep remote control characters out of the user's terminal and saved files."""
    return "".join(c for c in value if c in "\n\t" or not unicodedata.category(c).startswith("C"))


def clean_line(value: str) -> str:
    return " ".join(clean_text(value).split())


def topic_slug(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().lower()
    value = re.sub(r"[\s_]+", "-", value).strip("-")
    if not value or len(value) > 100 or not all(c.isalnum() or c == "-" for c in value):
        raise ReaderError("Use a topic name such as python, web-development, or machine learning.")
    return value


def safe_url(value: str, *, medium_only: bool = False) -> str:
    value = value.strip()
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        port = parts.port
    except ValueError as exc:
        raise ReaderError("That URL is not valid.") from exc
    if (
        parts.scheme != "https"
        or not host
        or parts.username
        or parts.password
        or port not in (None, 443)
        or any(c.isspace() for c in value)
        or clean_text(value) != value
    ):
        raise ReaderError("Use a complete HTTPS article URL without a username or password.")
    if medium_only and host != "medium.com" and not host.endswith(".medium.com"):
        raise ReaderError(
            "Direct reads support medium.com and its subdomains. "
            "Read custom-domain stories by their search result number."
        )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def article_key(url: str) -> str:
    """Medium's story ID survives tracking parameters, publication, and author URLs."""
    parts = urlsplit(url)
    match = re.search(r"(?:-|/)([a-f0-9]{12})/?$", parts.path)
    if match:
        return match.group(1)
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


@dataclass
class Article:
    title: str
    url: str
    author: str = "Unknown author"
    published: str = ""
    tags: list[str] | None = None
    markdown: str = ""
    source: str = "rss"
    note: str = FEED_NOTE
    claps: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def minutes(self) -> int:
        return max(1, round(len(self.markdown.split()) / 220))


def html_to_markdown(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style, nav, footer, button, form, iframe, svg, noscript"):
        node.decompose()
    for link in soup.find_all("a"):
        href = urljoin(base_url, link.get("href", ""))
        if urlsplit(href).scheme in ("https", "http", "mailto"):
            link["href"] = clean_text(href)
        else:
            link.attrs.pop("href", None)
    # Terminals cannot display these images. Preserve descriptive captions without trackers.
    for img in soup.find_all("img"):
        alt = clean_line(img.get("alt", ""))
        img.replace_with(f"[Image: {alt}]" if alt else "")
    for paragraph in soup.find_all("p"):
        if paragraph.get_text(" ", strip=True).startswith("Continue reading on "):
            paragraph.decompose()
    result = markdownify(str(soup), heading_style="ATX", bullets="-")
    return clean_text(result).strip()


def parse_feed(content: bytes) -> list[Article]:
    try:
        root = ElementTree.fromstring(content)
    except (ElementTree.ParseError, DefusedXmlException) as exc:
        raise ReaderError("Medium returned an invalid RSS feed. Try again later.") from exc
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise ReaderError("Medium returned a page instead of an RSS feed. Try again later.")
    result: list[Article] = []
    seen: set[str] = set()
    for item in channel.findall("item"):
        try:
            url = safe_url(item.findtext("link", ""))
        except ReaderError:
            continue
        key = article_key(url)
        if key in seen:
            continue
        seen.add(key)
        raw_date = item.findtext("pubDate", "")
        try:
            date = parsedate_to_datetime(raw_date).date().isoformat()
        except (ValueError, TypeError, OverflowError):
            date = clean_line(raw_date)
        body = item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
        snippet = not body
        body = body or item.findtext("description", "")
        result.append(
            Article(
                title=clean_line(item.findtext("title", "Untitled")),
                url=url,
                author=clean_line(item.findtext("{http://purl.org/dc/elements/1.1/}creator", ""))
                or "Unknown author",
                published=date,
                tags=[clean_line(node.text or "") for node in item.findall("category")],
                markdown=html_to_markdown(body, url),
                source="rss-preview" if snippet else "rss",
                note=(
                    "Topic-feed preview. Read online to fetch the article page."
                    if snippet
                    else FEED_NOTE
                ),
            )
        )
    return result


def parse_article(content: bytes, url: str) -> Article:
    soup = BeautifulSoup(content, "html.parser")
    article = soup.find("article")
    if article is None:
        raise ReaderError(
            "No readable article was returned. Medium may require sign-in or block this request. "
            "Try finding the story with 'medium-reader search TOPIC' instead."
        )

    def meta(name: str) -> str:
        node = soup.find("meta", attrs={"property": name})
        if node is None:
            node = soup.find("meta", attrs={"name": name})
        return clean_line(node.get("content", "")) if node else ""

    def paywalled(value: object) -> bool:
        if isinstance(value, list):
            return any(paywalled(v) for v in value)
        if isinstance(value, dict):
            accessible = value.get("isAccessibleForFree")
            if accessible is False or str(accessible).lower() == "false":
                return True
            return any(paywalled(v) for v in value.values() if isinstance(v, (list, dict)))
        return False

    restricted = bool(article.select_one('[data-testid="paywall"]'))
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            restricted = restricted or paywalled(json.loads(script.string or "{}"))
        except (json.JSONDecodeError, RecursionError):
            continue
    heading = article.find("h1")
    title = clean_line(heading.get_text(" ", strip=True)) if heading else meta("og:title")
    # Medium sections hold the story; header controls and recommendations live outside them.
    # Current Medium markup labels body paragraphs; keep their semantic siblings.
    # The generic section fallback supports older pages and nonstandard publications.
    body_paragraphs = article.select(".pw-post-body-paragraph")
    sections = article.find_all("section", recursive=True)
    sections = [section for section in sections if not section.find_parent("section")]
    if body_paragraphs:
        story_nodes = article.select(
            ".pw-post-body-paragraph, .pw-subtitle-paragraph, "
            "h2, h3, h4, h5, h6, blockquote, ul, ol, pre, figure"
        )
        story_ids = {id(node) for node in story_nodes}
        story_nodes = [
            node
            for node in story_nodes
            if not any(id(parent) in story_ids for parent in node.parents)
        ]
        sections = story_nodes
    body = BeautifulSoup(
        "".join(str(s) for s in sections) if sections else str(article), "html.parser"
    )
    for node in body.select(
        'h1, [hidden], [aria-hidden="true"], [data-testid="paywall"], [role="button"]'
    ):
        node.decompose()
    markdown = html_to_markdown(str(body), url)
    if not markdown:
        raise ReaderError("This story has no public article text. It may require a membership.")
    return Article(
        title=title or "Untitled",
        url=url,
        author=meta("author") or "Unknown author",
        published=meta("article:published_time")[:10],
        markdown=markdown,
        source="public-page",
        note=PREVIEW_NOTE if restricted else PAGE_NOTE,
    )


class MediumClient:
    def __init__(self, timeout: float = 20, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
            headers={
                "User-Agent": "medium-reader/0.2.0 (public RSS reader)",
                "Accept": "application/rss+xml, text/html;q=0.9, */*;q=0.1",
            },
        )

    def close(self) -> None:
        self.http.close()

    def fetch(self, url: str) -> tuple[bytes, str]:
        # Validate every redirect as well as the initial URL; never send cookies or credentials.
        for _ in range(6):
            url = safe_url(url, medium_only=True)
            try:
                with self.http.stream("GET", url) as response:
                    status = response.status_code
                    if status in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise ReaderError("Medium returned a redirect with no destination.")
                        url = urljoin(url, location)
                        continue
                    if status in (401, 403):
                        raise ReaderError(
                            "Medium blocked this request or requires sign-in. "
                            "Try a topic search, or use --offline for previously fetched content."
                        )
                    if status == 404:
                        raise ReaderError("Medium could not find that topic or article (HTTP 404).")
                    if status == 429:
                        raise ReaderError(
                            "Medium is rate limiting requests. Wait and try again later."
                        )
                    if status >= 400:
                        raise ReaderError(f"Medium returned HTTP {status}. Try again later.")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise ReaderError("Medium's response exceeded the 8 MB size limit.")
                        chunks.append(chunk)
                    return b"".join(chunks), str(response.url)
            except httpx.TimeoutException as exc:
                raise ReaderError(
                    "The request timed out. Try again or increase --timeout."
                ) from exc
            except httpx.HTTPError as exc:
                raise ReaderError(
                    "Could not connect to Medium. Check your connection, or use --offline."
                ) from exc
        raise ReaderError("Medium returned too many redirects.")

    def search(self, topic: str) -> list[Article]:
        data, _ = self.fetch(f"https://medium.com/feed/tag/{quote(topic_slug(topic))}")
        return parse_feed(data)

    def read(self, url: str) -> Article:
        try:
            data, resolved = self.fetch(url)
            return parse_article(data, resolved)
        except ReaderError as original_error:
            # An author's documented RSS feed can still serve a public story when
            # Medium blocks a direct HTML request. It never supplies locked text.
            parts = urlsplit(safe_url(url, medium_only=True))
            match = re.match(r"^/(@[^/]+)/", parts.path)
            if not match:
                raise
            author = unquote(match.group(1))
            try:
                data, _ = self.fetch(f"https://medium.com/feed/{quote(author, safe='@')}")
                for article in parse_feed(data):
                    if article_key(article.url) == article_key(url):
                        if article.source == "rss-preview":
                            article.note = f"Only an RSS preview is available. {original_error}"
                        return article
            except ReaderError:
                pass
            raise original_error
