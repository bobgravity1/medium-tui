# Medium Reader

Search Medium topics and read articles from your terminal. Python 3.10+; macOS,
Linux, and Windows. Topic search needs no API key or account. Personalized homepage browsing uses
your Medium sign-in in Chrome through the included extension. This is an independent reader, unaffiliated
with Medium.

## Install and start

On macOS or Linux, from this directory:

```sh
./install.sh
./medium-reader
```

The installer creates a project-local `.venv` and installs the package and its
dependencies there. It does not change your shell configuration or system Python.
The launcher works from any directory when called by its full path.

To use the commands without a path in your current terminal:

```sh
source .venv/bin/activate
medium-reader search python
medium-reader read 1 --pager
```

For a permanent command on your PATH, install this directory using either
[pipx](https://pipx.pypa.io/stable/installation/) or
[uv](https://docs.astral.sh/uv/guides/tools/):

```sh
pipx install .
# Alternatively:
uv tool install .
```

Both `medium-reader` and `medium-tui` are installed as equivalent commands.
The package has not been published to PyPI; install from this directory.

On Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install .
.\.venv\Scripts\medium-reader
```

## Visual reader

When run from a terminal, the interactive reader opens a two-pane interface:
library navigation is on the left and feed stories are on the right. Opening
an article replaces library navigation with its heading outline and shows the
article on the right.
That outline tracks your reading position, shows section-level progress, and
lets you jump directly to a section. Your place is saved locally for each
article and restored when you reopen it.

```sh
# Open the visual reader. A Medium account is only needed for /home and /favorites.
./medium-reader

# Open a topic directly in the visual reader.
./medium-reader search postgres --pick

# Keep the earlier line-based prompts instead.
./medium-reader --classic
```

Use the arrow keys or `j`/`k` to move, `Enter` to open a story or heading,
`Tab` to switch panes, `n` for more homepage stories (or page-down while
reading), `g`/`G` for the beginning/end, `w` to hide or show the sidebar, and
`Esc` to go back. Press `/` to enter a topic or one of the commands below:

```text
/home  /favorites  /search TOPIC  /recent  /popular
/login  /done  /logout  /back  /quit  /help
```

The visual interface uses muted Catppuccin-style colors and needs a terminal
that supports color and Unicode, including Ghostty. Non-interactive commands,
redirected output, and `--classic` retain the original plain CLI behavior.

Click a paragraph in an article to copy the whole paragraph as plain text.
It briefly becomes lighter and “Copied” appears at the bottom right. The reader
uses the native clipboard on macOS/Windows or `wl-copy`/`xclip` on Linux. If no
native clipboard tool is available, it sends an OSC 52 clipboard request to
the terminal and reports “Copy sent”; terminal policy determines whether that
request is accepted. Hold Shift to use your terminal's own text selection.

## Search and read

```sh
# A topic is a Medium tag. Spaces are converted to hyphens.
medium-reader search "machine learning"
medium-reader search web development --limit 5

# Filter the stories supplied by that topic's feed.
medium-reader search python --contains postgres

# Choose a numbered story without leaving the search command.
medium-reader search design --pick

# Numbers refer to the most recent search, even across terminal sessions.
medium-reader read 1
medium-reader read 2 --pager

# Read a public Medium article directly; replace the example URL.
medium-reader read 'https://medium.com/@author/story-title-ARTICLE_ID'

# Save a Markdown copy; existing files are protected unless --force is supplied.
medium-reader read 1 --save article.md

# Script-friendly output; diagnostics go to stderr.
medium-reader search python --json
medium-reader read 1 --format json
medium-reader read 1 --format markdown > article.md
medium-reader read 1 --format plain

# Read previously fetched content without network requests.
medium-reader search python --offline
medium-reader read 1 --offline
```

Run `medium-reader` with no arguments for the visual reader. Enter a topic,
then select a story with the arrow keys and Enter. Esc returns to your feed;
q exits. `--classic` uses the earlier numbered prompts and terminal pager.
With redirected input/output, the bare command prints help instead of waiting
for input. `--pick` requires an interactive terminal.

## Browse your personalized homepage

Start `./medium-reader`, then enter `/login`. This opens your system's default
browser. With Chrome as your default, it uses your normal Chrome account and
your existing Medium sign-in. Enter `/home` for your personalized feed or
`/favorites` for saved stories. Enter a topic normally for public RSS search.

You can also open the homepage directly from Ghostty:

```sh
./medium-reader home
```

The Chrome connection requires a one-time extension installation:

1. In Chrome, open `chrome://extensions` and enable Developer mode.
2. Choose **Load unpacked**, then select `src/medium_reader/chrome_extension`
   in this project. The browser connection page also shows its absolute path.
3. Enter `/login` in the reader. Reload the connection page if you installed
   the extension after opening it, then click **Connect and open Medium**.
4. Sign in on Medium if prompted. The reader recognizes the account and loads
   your homepage automatically. An already signed-in account needs no new login.

You can launch directly into the connection flow from Ghostty:

```sh
./medium-reader login
```

Keep the connected Chrome tab open while browsing. It exports only rendered
story cards through a local connection with a fresh token for this reader run.
Passwords and cookies remain in Chrome; the extension has no cookie, debugger,
or broad browsing-history permissions. `/done` remains available for a manual
connection check, but the visual reader normally detects completion itself.
Each new reader process needs a new connection; Chrome's Medium login persists.
Standalone `home --list`/`home --json` use `--offline` for cached cards; browse a
live account inside the connected reader. `/logout` disconnects this reader and
clears its feed lists without signing your account out of Chrome or ego-lite.

In homepage browsing, type `n` for the next page, `p` for the previous page, or
an article number to open the same article view used by search. Press `q` in the
article pager to return to that homepage page. Use `b` to return to topic search,
or `q` at the homepage prompt to quit. Ghostty's normal scrollback remains
available. The reader loads additional homepage cards when you advance past
those already loaded; numbers stay stable throughout that browsing session.

```sh
./medium-reader home --page-size 5
./medium-reader home --list --limit 20
./medium-reader home --json --limit 20
./medium-reader home --offline
./medium-reader favorites
./medium-reader logout
```

`--offline` lists the last fetched homepage cards without opening a browser.
An offline article must have been read and cached previously; a card alone does
not contain the story. Your homepage is cached separately from topic results.
`read 1` refers to the latest displayed search or homepage list. `--limit` applies
to one-shot `--list`/`--json` output; interactive browsing loads more on demand.
`/favorites` reads your default Medium Reading list in the connected reader.
`/logout` disconnects it. While browsing, `/recent`
sorts by the available publication dates and `/popular` sorts by visible clap
counts. Ranking is only possible when Medium includes that metadata in the cards.

This feature personalizes discovery. Opening a story still uses the existing
public article/RSS reader, so a homepage recommendation can lead to a
member-only preview. Homepage markup and loading behavior are controlled by
Medium; if it changes, the reader reports an error rather than substituting a
public or unrelated feed.

## What “search” and “read” mean here

Topic search uses Medium's documented `https://medium.com/feed/tag/TOPIC` RSS
endpoint. It returns the recent stories Medium includes in that feed; it is not
a full-text search of Medium's archive, and there is no feed pagination.
`--limit` caps the displayed results; it cannot retrieve more than the feed
supplies. In interactive search, `n` and `p` move through the available results;
`n` reports when Medium has no additional page to load. `/recent` sorts by feed
dates. `/popular` works only when the result source exposes clap counts.
`--contains` searches only the fetched titles and available text.

RSS is the interface Medium provides for reading its stories in other apps.
Using it avoids depending on Medium's private search API or browser markup for
topic discovery. Topic feeds often supply only a snippet. Selecting one of those results fetches
the public article page automatically. If Medium blocks the page, the reader
tries the author’s documented RSS feed when the URL identifies the author. If
only a cached snippet is available, it displays an explicit preview notice.

Medium says member-only stories are **not available in full through RSS**.
The reader labels feed content as possibly excerpted, and labels detected
member-only web pages as public previews. It does not sign in, access account
cookies, or unlock paid stories. Read the original URL on Medium for content
that requires your membership. Images appear as their supplied alt text;
interactive embeds are omitted. Code blocks and headings are retained.

Direct URL fetching supports `medium.com` and `*.medium.com`. Custom-domain
stories can be read from the content supplied in a topic feed by their result
number. Direct requests redirected to another domain are rejected. Medium may
block automated article-page requests even when its RSS feeds work; in that
case, try finding the story by topic instead. HTML extraction depends on
Medium's page structure and may need future maintenance.

Reference: [Medium's official RSS documentation](https://help.medium.com/hc/en-us/articles/214874118-Using-RSS-feeds-of-profiles-publications-and-topics).

## Cache and configuration

The local SQLite cache contains fetched article content, topic results,
personalized homepage card metadata, and the last displayed result list. A successful search stores the entire returned feed,
then applies your display filter. A new displayed list replaces the result
numbers. Search fetches fresh data unless `--offline` is supplied. Reading uses
cached article text when present; snippets trigger an article fetch unless
`--offline` is supplied. `read --refresh` requests the article again, falling
back to its author feed if available. A search failure is reported instead of silently showing stale results. If an
article fetch fails, an available cached preview is displayed with the reason;
`--refresh` reports an error if neither the page nor author feed supplies it.

The default directory follows your OS's cache convention through `platformdirs`:
`~/Library/Caches/medium-reader` on macOS,
`~/.cache/medium-reader` on Linux, and the local application cache on Windows.
Override it with `--data-dir /path/to/cache` or `MEDIUM_READER_HOME`.
The cache has no automatic expiry; delete its `reader.sqlite3` file while the
reader is closed to clear it.

`--timeout 30` changes the network timeout (default 20 seconds, maximum 120).
`--no-color` or the `NO_COLOR` environment variable disables colors. Read output
defaults to styled text in a terminal and Markdown when piped. `--format plain`
renders Markdown as plain terminal text. `--format json` emits article metadata
and the Markdown body. Network failures, blocked pages, missing results, and
file errors exit with status 1; invalid arguments exit with status 2; Ctrl-C
exits with status 130.

No telemetry or global browser configuration changes are made. Homepage browsing
uses the connected Chrome extension; topic search and article reading use HTTP. HTTP
responses are bounded to 8 MB, XML entities are disabled, and remote control
characters are removed before rendering. HTTP article requests stay on Medium
domains. The sign-in UI may take you to the identity provider you choose.

## Develop and test

```sh
./install.sh --dev
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Tests use synthetic RSS and HTML plus mocked HTTP requests; they do not depend
on Medium being reachable. Run `./install.sh` again after source changes to
update the project-local installation, or use an editable development install:

```sh
.venv/bin/python -m pip install -e '.[dev]'
```

The modules separate the request lifecycle into three small steps: `core.py`
fetches and parses content, `storage.py` remembers it, and `cli.py` handles commands
and rendering. `home.py` parses rendered feed cards and retains the legacy ego-lite
adapter; `chrome.py` provides the current Chrome connection. Tests exercise the
local bridge with synthetic cards and leave your real accounts signed in.

To uninstall the local installation, remove this project's `.venv` directory.
For a pipx/uv installation, use `pipx uninstall medium-tui` or
`uv tool uninstall medium-tui`. Cached articles are stored separately.
