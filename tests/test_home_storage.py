import pytest

from medium_reader.core import Article, ReaderError
from medium_reader.storage import Store


@pytest.fixture
def store(tmp_path):
    cache = Store(tmp_path)
    try:
        yield cache
    finally:
        cache.close()


def story(number, **kwargs):
    return Article(
        f"Story {number}",
        f"https://medium.com/@author/story-{number:012x}",
        **kwargs,
    )


def test_home_missing_has_actionable_error(store):
    with pytest.raises(ReaderError, match="medium-reader home.*online first"):
        store.home()


def test_empty_home_is_cached_across_sessions(tmp_path):
    cache = Store(tmp_path)
    cache.remember_home([])
    cache.close()
    reopened = Store(tmp_path)
    try:
        assert reopened.home() == []
    finally:
        reopened.close()


def test_favorites_and_browser_space_persist_separately(tmp_path):
    cache = Store(tmp_path)
    favorite = story(7, claps=1200)
    cache.remember_favorites([favorite])
    cache.set_state("home_browser_space", 42)
    cache.close()

    reopened = Store(tmp_path)
    empty = Store(tmp_path / "empty")
    try:
        assert reopened.favorites() == [favorite]
        assert reopened.state("home_browser_space") == 42
        with pytest.raises(ReaderError, match="offline saved stories"):
            empty.favorites()
    finally:
        reopened.close()
        empty.close()


def test_home_topic_and_displayed_results_are_independent(store):
    topic_story = story(1, markdown="Topic body")
    home_story = story(2, source="home-preview", markdown="Home snippet")
    store.remember_topic("python", [topic_story])
    store.remember_results([topic_story])

    store.remember_home([home_story])

    assert store.home() == [home_story]
    assert store.topic("python") == [topic_story]
    assert store.result(1) == topic_story
    store.remember_results([home_story])
    assert store.result(1) == home_story
    assert store.topic("python") == [topic_story]

    store.remember_topic("python", [])
    assert store.home() == [home_story]
    assert store.result(1) == home_story


def test_home_batch_preserves_order_and_replaces_previous_snapshot(store):
    first, second, third = (story(number) for number in range(1, 4))
    store.remember_home([second, first])
    store.remember_home([second, first, third])
    assert store.home() == [second, first, third]
    store.remember_home([])
    assert store.home() == []


@pytest.mark.parametrize(
    "source, markdown",
    [
        ("rss-preview", "A short snippet"),
        ("home-preview", "A short snippet"),
        ("rss-preview", ""),
        ("home-preview", ""),
        ("page", ""),
    ],
)
def test_home_refresh_preserves_full_body(store, source, markdown):
    full = story(1, source="page", markdown="The complete public article body.")
    store.put(full)
    card = story(1, source=source, markdown=markdown)
    card.url += "?source=home"

    store.remember_home([card])

    assert store.home() == [full]
    assert store.get(card.url) == full


def test_empty_card_preserves_an_existing_preview(store):
    preview = story(1, source="rss-preview", markdown="An available preview.")
    store.put(preview)
    store.remember_home([story(1, source="home-preview")])
    assert store.home() == [preview]


def test_full_article_replaces_home_preview(store):
    preview = story(1, source="home-preview", markdown="A short snippet.")
    store.remember_home([preview])
    full = story(1, source="page", markdown="The complete public article body.")
    store.put(full)
    assert store.home() == [full]


def test_new_home_preview_can_refresh_existing_preview(store):
    store.put(story(1, source="home-preview", markdown="Old preview."))
    refreshed = story(1, source="home-preview", markdown="New preview.")
    store.remember_home([refreshed])
    assert store.home() == [refreshed]
