import json
import subprocess

import pytest

from medium_reader import home
from medium_reader.core import Article, ReaderError
from medium_reader.home import HOME_NOTE, MARKER, HomeFeed, merge_home, parse_home

URL = "https://medium.com/@ada/database-connections-123456abcdef"


def card(url=URL, title="Database connections", *, author=True):
    byline = '<a href="/@ada">Ada Lovelace</a>' if author else ""
    return (
        f'<article>{byline}<a href="{url}"><h2>{title}</h2></a>'
        '<time datetime="2026-10-09T12:00:00Z">Oct 9</time></article>'
    )


def test_parse_home_extracts_cards_and_deduplicates_story_ids():
    html = (
        "<main>"
        + card(title="Database &amp; connections")
        + card(URL + "?source=home", title="Duplicate card")
        + '<div data-testid="post-preview"><h3><a '
        'href="/@grace/another-story-abcdef123456">Another story</a></h3></div>'
        "</main>" + card("https://medium.com/@author/outside-main-111111111111")
    )
    articles = parse_home(html)
    assert len(articles) == 2
    first, second = articles
    assert first.title == "Database & connections"
    assert first.url == URL
    assert first.author == "Ada Lovelace"
    assert first.published == "2026-10-09"
    assert first.source == "home-preview"
    assert first.markdown == ""
    assert first.note == HOME_NOTE
    assert second.url == "https://medium.com/@grace/another-story-abcdef123456"
    assert second.author == "Unknown author"


def test_parse_home_reads_visible_compact_clap_count_and_card_date():
    html = (
        '<article><p>Mar 8</p><a href="/@ada/popular-story-123456abcdef">'
        "<h2>Popular story</h2>"
        "<svg><desc>A clap icon</desc></svg>3.5K</a></article>"
    )
    article = parse_home(html)[0]
    assert article.published == "Mar 8"
    assert article.claps == 3500


def test_parse_home_handles_heading_and_story_link_as_siblings():
    articles = parse_home(
        "<article><h2>A rendered title</h2>"
        '<a href="/@ada">Ada</a>'
        '<a href="/@ada/story-123456abcdef">Read story</a></article>'
    )
    assert len(articles) == 1
    assert articles[0].title == "A rendered title"
    assert articles[0].url == "https://medium.com/@ada/story-123456abcdef"


def test_parse_home_allows_custom_domain_publication_cards():
    url = "https://publication.example/story-123456abcdef"
    assert parse_home(card(url))[0].url == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///tmp/story-123456abcdef",
        "http://external.example/story-123456abcdef",
        "https://user:password@external.example/story-123456abcdef",
        "https://external.example:8443/story-123456abcdef",
        "https://[broken/story-123456abcdef",
        "https://external.example/a story-123456abcdef",
    ],
)
def test_parse_home_ignores_unsafe_or_malformed_links(url):
    assert parse_home(card(url)) == []


def test_parse_home_ignores_non_story_navigation_and_incomplete_cards():
    html = (
        card("/me/following", title="Following")
        + card("https://external.example/help", title="Help")
        + '<article><a href="/@ada/story-123456abcdef">No heading</a></article>'
        + "<article><h2>Heading without a link</h2></article>"
        + card(title=" \n ")
    )
    assert parse_home(html) == []


def test_parse_home_strips_control_characters_from_display_text():
    articles = parse_home(card(title="A\x1b title\nwith whitespace"))
    assert articles[0].title == "A title with whitespace"


def test_merge_home_appends_only_new_story_ids_without_renumbering():
    first = Article("First", URL)
    second = Article("Second", "https://medium.com/@ada/second-abcdef123456")
    duplicate = Article("New title for first", "https://ada.medium.com/other-123456abcdef")
    third = Article("Third", "https://publication.example/third-111111111111")
    existing = [first, second]

    merged = merge_home(existing, [duplicate, third, second, third])

    assert merged == [first, second, third]
    assert existing == [first, second]
    assert merged[0] is first
    assert merge_home(merged, []) == merged


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setattr(home.shutil, "which", lambda _: "/isolated task/bin/ego-browser")
    calls = []
    replies = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, subprocess.CompletedProcess):
            return reply
        return subprocess.CompletedProcess(
            args, 0, stdout=f"bridge log\n{MARKER}{json.dumps(reply)}\n", stderr=""
        )

    monkeypatch.setattr(home.subprocess, "run", run)
    return HomeFeed(space_id=17), calls, replies


def test_bridge_uses_argv_without_a_shell(bridge):
    feed, calls, replies = bridge
    replies.append({"answer": 42})
    assert feed._run("const result = {answer: 42};") == {"answer": 42}
    args, kwargs = calls[0]
    assert args[:3] == ["/isolated task/bin/ego-browser", "nodejs", "-e"]
    assert kwargs.get("shell", False) is False
    assert "const result = {answer: 42};" in args[3]
    assert MARKER in args[3]
    assert "JSON.stringify(result)" in args[3]
    assert "input" not in kwargs
    assert kwargs["text"] is True
    assert kwargs["capture_output"] is True
    assert 0 < kwargs["timeout"] <= 60


def test_keep_open_preserves_browser_space_for_later_cli_commands(bridge):
    feed, calls, _ = bridge
    feed.keep_open = True
    feed.close()
    assert feed.space_id == 17
    assert calls == []


def test_bridge_parses_last_result_marker_amid_other_output(bridge):
    feed, _, replies = bridge
    replies.append(
        subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                f'{MARKER}{{"old": true}}\n'
                'diagnostic: {"unrelated": true}\n'
                f'{MARKER}{{"answer": 42}}\n'
                "finished\n"
            ),
            stderr="some diagnostic output",
        )
    )
    assert feed._run("const result = {};") == {"answer": 42}


@pytest.mark.parametrize("payload", ["not json", "[]", "null", '"text"'])
def test_bridge_invalid_result_is_an_actionable_error(bridge, payload):
    feed, calls, replies = bridge
    replies.append(subprocess.CompletedProcess([], 0, stdout=MARKER + payload, stderr=""))
    with pytest.raises(ReaderError, match="ego-browser"):
        feed._run("const result = {};")
    assert feed.can_control is False
    feed.close()
    assert len(calls) == 1


def test_bridge_missing_result_is_an_actionable_error(bridge):
    feed, _, replies = bridge
    replies.append(subprocess.CompletedProcess([], 1, stdout="startup failed", stderr="failed"))
    with pytest.raises(ReaderError, match="ego-browser returned no result"):
        feed._run("const result = {};")
    assert feed.can_control is False


def test_bridge_explicit_error_relinquishes_control_and_sanitizes_message(bridge):
    feed, calls, replies = bridge
    replies.append({"error": "User took\x1b control\nfinish sign-in"})
    with pytest.raises(ReaderError, match="User took control finish sign-in"):
        feed._run("const result = {};")
    assert feed.can_control is False
    feed.close()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error",
    [OSError("not available"), subprocess.TimeoutExpired("ego-browser", 45)],
)
def test_bridge_transport_failure_is_actionable_and_relinquishes_control(bridge, error):
    feed, _, replies = bridge
    replies.append(error)
    with pytest.raises(ReaderError, match="Could not reach ego-browser"):
        feed._run("const result = {};")
    assert feed.can_control is False


def test_login_handoff_stays_under_user_control_until_explicit_confirmation(bridge):
    feed, calls, replies = bridge
    replies.append({"handedOff": True})
    feed.begin_login()
    assert feed.can_control is False
    assert "task.handOff()" in calls[0][0][3]

    with pytest.raises(ReaderError, match="under user control"):
        feed.load_more()
    feed.close()
    assert len(calls) == 1

    replies.extend([{"resumed": True}, {"signedOut": False, "html": card()}])
    articles = feed.confirm_login()
    assert articles[0].url == URL
    assert feed.can_control is True
    assert "takeOverTaskSpace(17)" in calls[1][0][3]
    assert "takeOverTaskSpace" not in calls[2][0][3]

    replies.append({"signedOut": False, "html": card()})
    assert feed.load_more()[0].url == URL
    replies.append({"closed": True})
    feed.close()
    assert feed.space_id is None
    assert [i for i, (args, _) in enumerate(calls) if "takeOverTaskSpace" in args[3]] == [1]


def test_confirm_login_requires_an_existing_sign_in_space(bridge):
    feed, calls, _ = bridge
    feed.space_id = None
    with pytest.raises(ReaderError, match="Start sign-in first"):
        feed.confirm_login()
    assert calls == []


@pytest.mark.parametrize(
    "result, message",
    [
        ({"signedOut": True, "html": ""}, "medium-reader login"),
        ({"signedOut": False, "html": "<main></main>"}, "No homepage stories"),
    ],
)
def test_home_load_reports_signed_out_and_empty_pages(bridge, result, message):
    feed, _, replies = bridge
    replies.append(result)
    with pytest.raises(ReaderError, match=message):
        feed.load_more()


def test_empty_saved_reading_list_is_a_valid_result(bridge):
    feed, _, replies = bridge
    replies.append(
        {
            "signedOut": False,
            "html": (
                "<main><p>Add your favorite stories to your list. "
                "Simply click the save icon</p></main>"
            ),
        }
    )
    assert feed.load_favorites() == []


def test_missing_ego_browser_explains_installation_and_login(monkeypatch):
    monkeypatch.setattr(home.shutil, "which", lambda _: None)
    monkeypatch.setattr(home.Path, "is_file", lambda _: False)
    with pytest.raises(ReaderError, match="Install ego-lite.*medium-reader login"):
        HomeFeed()


def test_parse_home_ignores_malformed_author_links_without_losing_the_story():
    html = card().replace("<article>", '<article><a href="https://[broken/">Broken author link</a>')
    articles = parse_home(html)
    assert len(articles) == 1
    assert articles[0].author == "Ada Lovelace"
    assert articles[0].url == URL


@pytest.mark.parametrize("stdout", ["", "ego-browser diagnostic output\n"])
def test_bridge_parses_result_from_stderr_without_leaking_output(bridge, capsys, stdout):
    feed, _, replies = bridge
    replies.append(
        subprocess.CompletedProcess(
            [],
            0,
            stdout=stdout,
            stderr=f'bridge diagnostic\n{MARKER}{{"answer": 42}}\nfinished\n',
        )
    )
    assert feed._run("const result = {};") == {"answer": 42}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
