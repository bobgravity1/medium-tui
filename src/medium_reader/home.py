"""Optional signed-in homepage access through ego-lite's isolated browser.

The bridge reads rendered story cards only. Browser credentials stay in ego-lite.
Topic search and article HTTP requests do not depend on this module.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .core import Article, ReaderError, article_key, clean_line, safe_url

MARKER = "MEDIUM_READER_RESULT="
HOME_URL = "https://medium.com/"
HOME_NOTE = "Homepage preview. Read online to fetch the article using the existing reader."


def parse_home(content: str) -> list[Article]:
    """Extract rendered story cards, without reading embedded application state."""
    soup = BeautifulSoup(content, "html.parser")
    root = soup.find("main") or soup
    articles: list[Article] = []
    seen: set[str] = set()
    for card in root.select('article, [data-testid="post-preview"]'):
        heading = card.find(["h2", "h3"])
        if heading is None:
            continue
        link = heading.find_parent("a", href=True) or heading.find("a", href=True)
        if link is None:
            link = next(
                (
                    a
                    for a in card.find_all("a", href=True)
                    if re.search(r"(?:-|/)[a-f0-9]{12}(?:[/?#]|$)", a["href"])
                ),
                None,
            )
        if link is None:
            continue
        try:
            url = safe_url(urljoin(HOME_URL, link["href"]))
        except (ReaderError, ValueError):
            continue
        # Only story-shaped links can become selectable results.
        key = article_key(url)
        if not re.fullmatch(r"[a-f0-9]{12}", key) or key in seen:
            continue
        title = clean_line(heading.get_text(" ", strip=True))
        if not title:
            continue
        seen.add(key)
        author = "Unknown author"
        for candidate in card.find_all("a", href=True):
            try:
                path = urlsplit(urljoin(HOME_URL, candidate["href"])).path
            except ValueError:
                continue
            if re.fullmatch(r"/@[^/]+/?", path):
                label = clean_line(candidate.get_text(" ", strip=True))
                if label:
                    author = label
                    break
        timestamp = card.find("time")
        published = ""
        if timestamp:
            published = clean_line(timestamp.get("datetime", "")[:10]) or clean_line(
                timestamp.get_text(" ", strip=True)
            )
        if not published:
            date_label = re.search(
                r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}\b"
                r"|\b\d+\s*(?:min|h|d|w|wk|mo|y|yr)\s*ago\b",
                card.get_text(" ", strip=True),
                re.I,
            )
            if date_label:
                published = date_label.group(0)
        claps = None
        clap_label = next(
            (
                node
                for node in card.find_all(["svg", "img"])
                if "a clap icon" in clean_line(node.get_text(" ", strip=True)).casefold()
                or "a clap icon" in node.get("alt", "").casefold()
            ),
            None,
        )
        clap_anchor = clap_label.find_parent("a") if clap_label else None
        metric_text = clap_anchor.get_text(" ", strip=True) if clap_anchor else ""
        metric = re.search(r"A clap icon\s*([\d,.]+\s*[KMB]?)", metric_text, re.I)
        if metric:
            value = metric.group(1).replace(",", "").replace(" ", "").upper()
            multiplier = 1
            if value[-1:] in {"K", "M", "B"}:
                multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[value[-1]]
                value = value[:-1]
            try:
                claps = int(float(value) * multiplier)
            except ValueError:
                pass
        articles.append(
            Article(
                title=title,
                url=url,
                author=author,
                published=published,
                source="home-preview",
                note=HOME_NOTE,
                claps=claps,
            )
        )
    return articles


def merge_home(existing: list[Article], incoming: list[Article]) -> list[Article]:
    """Append new cards without changing the numbers assigned to existing cards."""
    result = list(existing)
    seen = {article_key(article.url) for article in existing}
    for article in incoming:
        key = article_key(article.url)
        if key not in seen:
            result.append(article)
            seen.add(key)
    return result


class HomeFeed:
    def __init__(self, *, space_id: int | None = None, keep_open: bool = False):
        executable = shutil.which("ego-browser")
        fallback = Path.home() / ".local/bin/ego-browser"
        if not executable and fallback.is_file():
            executable = str(fallback)
        if not executable:
            raise ReaderError(
                "Personalized home needs ego-lite and its ego-browser command. "
                "Install ego-lite from https://lite.ego.app/, then run 'medium-reader login'. "
                "Topic search still works without it."
            )
        self.executable = executable
        self.space_id = space_id
        self.keep_open = keep_open
        self.can_control = True
        self.loaded = False

    def _run(self, code: str) -> dict:
        script = (
            "try {\n"
            + code
            + f"\nconsole.log({json.dumps(MARKER)} + JSON.stringify(result));\n"
            + "} catch (error) {\n"
            + f"console.log({json.dumps(MARKER)} + JSON.stringify({{error: String(error)}}));\n"
            + "}\n"
        )
        try:
            process = subprocess.run(
                [self.executable, "nodejs", "-e", script],
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.can_control = False
            raise ReaderError("Could not reach ego-browser. Open ego-lite and try again.") from exc
        for line in reversed((process.stdout + "\n" + process.stderr).splitlines()):
            if not line.startswith(MARKER):
                continue
            try:
                result = json.loads(line[len(MARKER) :])
            except json.JSONDecodeError:
                break
            if not isinstance(result, dict):
                break
            if result.get("error"):
                # A user takeover must never be undone automatically or retried in a new space.
                self.can_control = False
                raise ReaderError(
                    "ego-browser could not complete the request. Check its window; if you took "
                    "control, finish there before retrying. " + clean_line(result["error"])[:250]
                )
            return result
        self.can_control = False
        raise ReaderError("ego-browser returned no result. Open ego-lite and try again.")

    def _start(self) -> None:
        if not self.can_control:
            raise ReaderError("The browser is under user control; complete sign-in first.")
        if self.space_id is None:
            name = f"Medium Reader {uuid.uuid4().hex[:8]}"
            result = self._run(
                f"const task = await taskSpace({json.dumps(name)});\n"
                "const result = {spaceId: task.spaceId};"
            )
            self.space_id = result["spaceId"]

    def load_more(self) -> list[Article]:
        self._start()
        if self.loaded:
            action = """
const before = await page.evaluate(() => document.body.scrollHeight);
await page.keyboard.press("End");
try {
  await page.waitForFunction((height) => document.body.scrollHeight > height,
                            before, {timeout: 5000});
} catch (_) { /* No new cards is a valid end-of-feed result. */ }
"""
        else:
            action = """
await page.goto("https://medium.com/");
try {
  // Medium hydrates homepage cards after the initial document load.
  await page.waitForSelector('main [data-testid="post-preview"]', {timeout: 20000});
} catch (_) { /* The snapshot below reports sign-in or an empty feed clearly. */ }
"""
        result = self._run(f"""
const task = await taskSpace({self.space_id});
const page = task.page("p1");
{action}
const result = await page.evaluate(() => {{
  const root = (document.querySelector("main") || document.body).cloneNode(true);
  root.querySelectorAll("script, style, nav, footer, form, [hidden]").forEach(n => n.remove());
  const signedOut = [...document.querySelectorAll("a")].some(a =>
    a.textContent.trim().toLowerCase() === "sign in");
  return {{html: root.outerHTML, signedOut}};
}});
""")
        self.loaded = True
        if result["signedOut"]:
            raise ReaderError("Sign in to your Medium account first: medium-reader login")
        articles = parse_home(result["html"])
        if not articles:
            raise ReaderError(
                "No homepage stories were found. Check that Medium is signed in in ego-lite. "
                "The feed may still be loading or Medium's layout may have changed."
            )
        return articles

    def load_favorites(self) -> list[Article]:
        """Read stories from the account's default Reading list."""
        self._start()
        result = self._run(f"""
const task = await taskSpace({self.space_id});
const page = task.page("p1");
await page.goto("https://medium.com/me/lists");
try {{
  await page.waitForSelector('a[href*="/list/reading-list"]', {{timeout: 15000}});
}} catch (_) {{}}
const listUrl = await page.evaluate(() =>
  [...document.querySelectorAll('a[href*="/list/reading-list"]')]
    .map(a => a.href).find(href => href.includes("/list/reading-list")) || ""
);
if (!listUrl) throw new Error("Could not find your Reading list. Check that Medium is signed in.");
await page.goto(listUrl);
try {{
  await page.waitForSelector('main [data-testid="post-preview"], main article', {{timeout: 10000}});
}} catch (_) {{}}
const result = await page.evaluate(() => {{
  const root = (document.querySelector("main") || document.body).cloneNode(true);
  root.querySelectorAll("script, style, nav, footer, form, [hidden]").forEach(n => n.remove());
  const signedOut = [...document.querySelectorAll("a")].some(a =>
    a.textContent.trim().toLowerCase() === "sign in");
  return {{html: root.outerHTML, signedOut}};
}});
""")
        if result["signedOut"]:
            raise ReaderError("Sign in to your Medium account first: medium-reader login")
        articles = parse_home(result["html"])
        if not articles:
            # An empty Reading list is a valid, useful state.
            empty_text = result["html"].casefold()
            if "no stories" in empty_text or "add your favorite stories to your list" in empty_text:
                return []
            raise ReaderError(
                "Could not read your saved stories. Medium's Reading list layout may have changed."
            )
        return articles

    def logout(self) -> None:
        """Sign out through Medium's visible account menu."""
        self._start()
        self._run(f"""
const task = await taskSpace({self.space_id});
const page = task.page("p1");
await page.goto("https://medium.com/");
await page.click('button[aria-label="user options menu"]');
const result = {{snapshot: await page.snapshot()}};
""")
        menu = self._run(f"""
const task = await taskSpace({self.space_id});
const page = task.page("p1");
const snapshot = await page.snapshot();
if (!snapshot.toLowerCase().includes("sign out")) {{
  throw new Error("Medium's sign-out option was not visible.");
}}
const result = {{ready: true}};
""")
        if not menu.get("ready"):
            raise ReaderError("Could not open Medium's sign-out menu.")
        self._run(f"""
const task = await taskSpace({self.space_id});
const page = task.page("p1");
await page.click('loc=role:button[name*="Sign out"]');
await task.finish({{keep: []}});
const result = {{signedOut: true}};
""")
        self.space_id = None

    def begin_login(self) -> None:
        self._start()
        self._run(f"""
const task = await taskSpace({self.space_id});
const page = task.page("p1");
await page.goto("https://medium.com/");
await task.handOff();
const result = {{handedOff: true}};
""")
        self.can_control = False

    def confirm_login(self) -> list[Article]:
        if self.space_id is None:
            raise ReaderError("Start sign-in first.")
        # Called only after the user explicitly confirms in the terminal.
        self._run(f"""
const task = await takeOverTaskSpace({self.space_id});
const result = {{resumed: true}};
""")
        self.can_control = True
        self.loaded = False
        return self.load_more()

    def close(self) -> None:
        if self.space_id is not None and self.can_control and not self.keep_open:
            self._run(f"""
const task = await taskSpace({self.space_id});
await task.finish({{keep: []}});
const result = {{closed: true}};
""")
            self.space_id = None

    def __enter__(self) -> HomeFeed:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.close()
        except ReaderError:
            if exc is None:
                raise
