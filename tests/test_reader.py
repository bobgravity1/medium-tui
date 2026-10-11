import io
import json

import httpx
import pytest
from rich.console import Console

from medium_reader.cli import App, main, parser
from medium_reader.core import (
    Article,
    MediumClient,
    ReaderError,
    article_key,
    html_to_markdown,
    parse_article,
    parse_feed,
    topic_slug,
)
from medium_reader.storage import Store

URL = "https://medium.com/@author/understanding-python-123456abcdef"
FEED = f'''<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
 <title>Python on Medium</title><item>
 <title>Python &amp; databases</title><link>{URL}?source=rss</link>
 <dc:creator>Ada</dc:creator><pubDate>Fri, 09 Oct 2026 10:00:00 GMT</pubDate>
 <category>python</category><content:encoded><![CDATA[
 <h2>Connections</h2><p>Keep <strong>connections</strong> short.</p>
 <pre>print("hello")\nprint("world")</pre>
 <p>Continue reading on <a href="{URL}">Medium</a> »</p>
 ]]></content:encoded></item></channel></rss>'''.encode()


def test_feed_content_and_metadata():
    (article,) = parse_feed(FEED)
    assert article.title == "Python & databases"
    assert article.author == "Ada"
    assert article.published == "2026-10-09"
    assert article.tags == ["python"]
    assert "## Connections" in article.markdown
    assert "**connections**" in article.markdown
    assert 'print("hello")\nprint("world")' in article.markdown
    assert "Continue reading" not in article.markdown
    assert "excerpt" in article.note


def test_feed_deduplicates_story_ids():
    item = FEED[FEED.index(b"<item>") : FEED.index(b"</item>") + 7]
    duplicate = item.replace(b"?source=rss", b"?source=other")
    assert len(parse_feed(FEED.replace(b"</channel>", duplicate + b"</channel>"))) == 1


@pytest.mark.parametrize(
    "content",
    [
        b"broken",
        b"<html>Blocked</html>",
        b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel>&x;</channel></rss>',
    ],
)
def test_invalid_or_unsafe_feed(content):
    with pytest.raises(ReaderError):
        parse_feed(content)


def test_empty_feed_is_not_an_error():
    assert parse_feed(b"<rss><channel/></rss>") == []


def test_html_conversion_drops_scripts_and_unsafe_links():
    converted = html_to_markdown(
        '<script>evil()</script><p>Hi\x1b! <a href="javascript:alert(1)">click</a></p>'
        '<img src="https://tracker.example/pixel" alt="A chart"><a href="/about">About</a>',
        URL,
    )
    assert "evil" not in converted
    assert "javascript" not in converted
    assert "\x1b" not in converted
    assert "tracker.example" not in converted
    assert "A chart" in converted
    assert "https://medium.com/about" in converted


def test_public_page_extracts_article_and_marks_preview():
    data = b"""<html><head><meta name="author" content="Grace">
    <meta property="article:published_time" content="2026-10-09T12:00:00Z">
    <script type="application/ld+json">{"@graph":[{"isAccessibleForFree":false}]}</script>
    </head><body><nav>Navigation</nav><article><h1>A story</h1>
    <button>Follow</button><section><p>Public introduction.</p>
    <div hidden>Hidden text</div><div data-testid="paywall">Subscribe</div>
    </section></article><footer>Recommendations</footer></body></html>"""
    article = parse_article(data, URL)
    assert article.title == "A story"
    assert article.author == "Grace"
    assert article.published == "2026-10-09"
    assert article.markdown == "Public introduction."
    assert "preview" in article.note


def test_challenge_page_is_not_mistaken_for_article():
    with pytest.raises(ReaderError, match="No readable article"):
        parse_article(b"<html><h1>Just a moment</h1></html>", URL)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Machine Learning", "machine-learning"),
        (" web_development ", "web-development"),
        ("日本語", "日本語"),
    ],
)
def test_topic_normalization(value, expected):
    assert topic_slug(value) == expected


@pytest.mark.parametrize("value", ["", "../../secret", "python?x=y", "---"])
def test_invalid_topics(value):
    with pytest.raises(ReaderError):
        topic_slug(value)


@pytest.mark.parametrize(
    "status, message",
    [(403, "blocked"), (404, "could not find"), (429, "rate limiting"), (503, "HTTP 503")],
)
def test_http_errors(status, message):
    client = MediumClient(transport=httpx.MockTransport(lambda _: httpx.Response(status)))
    try:
        with pytest.raises(ReaderError, match=message):
            client.search("python")
    finally:
        client.close()


def test_redirect_cannot_leave_medium():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://localhost/private"})

    client = MediumClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ReaderError, match="Direct reads support"):
            client.fetch(URL)
        assert len(requests) == 1
    finally:
        client.close()


def test_medium_redirect_and_topic_url():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "https://medium.com/feed/tag/python/"})
        return httpx.Response(200, content=FEED)

    client = MediumClient(transport=httpx.MockTransport(handler))
    try:
        assert client.search("python")[0].author == "Ada"
        assert requests[0] == "https://medium.com/feed/tag/python"
    finally:
        client.close()


def test_timeout_is_actionable():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = MediumClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ReaderError, match="timed out"):
            client.search("python")
    finally:
        client.close()


def test_search_read_and_offline_across_sessions(tmp_path):
    store = Store(tmp_path)
    client = MediumClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=FEED))
    )
    try:
        app = App(client, store, Console(file=io.StringIO()))
        assert len(app.search("python", contains="connections")) == 1
        assert app.read("1").author == "Ada"
    finally:
        store.close()
        client.close()

    def no_network(_):
        pytest.fail("Offline reads must not make requests")

    store = Store(tmp_path)
    client = MediumClient(transport=httpx.MockTransport(no_network))
    try:
        app = App(client, store, Console(file=io.StringIO()))
        assert app.read(URL, offline=True).author == "Ada"
        assert len(app.search("python", offline=True)) == 1
        assert app.search("python", contains="absent", offline=True) == []
        with pytest.raises(ReaderError, match="No result"):
            app.read("1")
        with pytest.raises(ReaderError, match="No offline results"):
            app.search("unknown-topic", offline=True)
    finally:
        store.close()
        client.close()


def test_common_flags_work_before_or_after_command():
    args = parser().parse_args(["--timeout", "7", "search", "python", "--no-color"])
    assert args.timeout == 7
    assert args.no_color


def seed(tmp_path):
    store = Store(tmp_path)
    article = Article("Story", URL, "Ada", markdown="Read **this**.")
    store.remember_topic("python", [article])
    store.remember_results([article])
    store.close()


def test_cli_json_and_export(tmp_path, capsys):
    seed(tmp_path)
    assert main(["--data-dir", str(tmp_path), "search", "python", "--offline", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["number"] == 1
    assert data[0]["title"] == "Story"
    output = tmp_path / "story.md"
    args = [
        "--data-dir",
        str(tmp_path),
        "read",
        "1",
        "--offline",
        "--format",
        "json",
        "--save",
        str(output),
    ]
    assert main(args) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["markdown"] == "Read **this**."
    assert "Saved Markdown" in captured.err
    assert output.read_text().startswith("# Story")
    assert main(args) == 1
    assert "already exists" in capsys.readouterr().err
    assert main(args + ["--force"]) == 0


def test_cli_error_on_stderr(tmp_path, capsys):
    assert main(["--data-dir", str(tmp_path), "read", "3", "--offline"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "No result" in captured.err


def test_tracking_parameters_do_not_change_cache_identity():
    assert article_key(URL + "?source=abc") == article_key(URL + "?source=def")


def test_topic_description_is_marked_as_preview():
    feed = FEED.replace(b"content:encoded", b"description")
    (article,) = parse_feed(feed)
    assert article.source == "rss-preview"
    assert "preview" in article.note.lower()


def test_modern_article_preserves_headings_lists_and_code_without_byline():
    page = b"""<article><h1>Story</h1><section>
    <p>Follow this author</p><h2>A heading</h2>
    <p class="pw-post-body-paragraph">Real text.</p>
    <ul><li class="pw-post-body-paragraph">One list item</li></ul>
    <pre>first line\nsecond line</pre>
    </section></article>"""
    article = parse_article(page, URL)
    assert "Follow this author" not in article.markdown
    assert "## A heading" in article.markdown
    assert article.markdown.count("One list item") == 1
    assert "first line\nsecond line" in article.markdown


def test_blocked_article_uses_public_author_feed():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if request.url.path.startswith("/feed/"):
            return httpx.Response(200, content=FEED)
        return httpx.Response(403)

    client = MediumClient(transport=httpx.MockTransport(handler))
    try:
        article = client.read(URL)
        assert article.source == "rss"
        assert "connections" in article.markdown
        assert requests[-1] == "https://medium.com/feed/@author"
    finally:
        client.close()


def test_read_preview_fetches_article_then_preserves_it_on_new_search(tmp_path):
    preview_feed = FEED.replace(b"content:encoded", b"description")
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if request.url.path.startswith("/feed/"):
            return httpx.Response(200, content=preview_feed)
        return httpx.Response(
            200,
            content=b"<article><h1>Full story</h1>"
            b"<section><p>Complete public body.</p></section></article>",
        )

    store = Store(tmp_path)
    client = MediumClient(transport=httpx.MockTransport(handler))
    try:
        app = App(client, store, Console(file=io.StringIO()))
        assert app.search("python")[0].source == "rss-preview"
        assert app.read("1").markdown == "Complete public body."
        assert len(requests) == 2
        app.search("python")
        assert app.read("1", offline=True).markdown == "Complete public body."
        assert len(requests) == 3
    finally:
        store.close()
        client.close()


def test_blocked_fetch_reports_cached_preview_and_refresh_error(tmp_path):
    store = Store(tmp_path)
    article = Article("Story", URL, markdown="A snippet", source="rss-preview")
    store.remember_topic("python", [article])
    store.remember_results([article])
    client = MediumClient(transport=httpx.MockTransport(lambda _: httpx.Response(403)))
    try:
        app = App(client, store, Console(file=io.StringIO()))
        result = app.read("1")
        assert result.markdown == "A snippet"
        assert "Only the cached topic preview" in result.note
        assert "blocked" in result.note
        with pytest.raises(ReaderError, match="blocked"):
            app.read("1", refresh=True)
    finally:
        store.close()
        client.close()


def test_response_size_is_bounded(monkeypatch):
    monkeypatch.setattr("medium_reader.core.MAX_RESPONSE_BYTES", 8)
    client = MediumClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 9))
    )
    try:
        with pytest.raises(ReaderError, match="size limit"):
            client.fetch(URL)
    finally:
        client.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://medium.com.evil.test/story",
        "http://medium.com/story",
        "https://user:secret@medium.com/story",
        "https://medium.com:1234/story",
        "file:///tmp/story",
    ],
)
def test_invalid_urls_make_no_requests(url):
    def no_network(_):
        pytest.fail("Invalid URLs must be rejected before making a request")

    client = MediumClient(transport=httpx.MockTransport(no_network))
    try:
        with pytest.raises(ReaderError):
            client.fetch(url)
    finally:
        client.close()
