"""Fetch trending topics from Google Trends RSS. Stdlib only, no API key.

There is no generally available official Trends API; the RSS feed is the
scrappy, dependency-free source. Titles come back raw and messy (lowercase
names, mixed languages) — that's fine, the trend_research skill's job is
to extract the guessable core concept from noisy input.
"""
import sys
import urllib.request
import xml.etree.ElementTree as ET

TRENDS_RSS = "https://trends.google.com/trending/rss?geo={geo}"

# AM first (our audience), US as a broader fallback — Armenia's daily list
# can be short and dominated by one news story.
DEFAULT_GEOS = ("AM", "US")


def fetch_topics(geo: str, timeout: int = 10) -> list[str]:
    """Trending search titles for one country code, newest first."""
    req = urllib.request.Request(
        TRENDS_RSS.format(geo=geo),
        headers={"User-Agent": "Mozilla/5.0"},  # default UA gets blocked
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        root = ET.fromstring(resp.read())
    titles = (item.findtext("title", "").strip() for item in root.iter("item"))
    return [t for t in titles if t]


def fetch_all(geos=DEFAULT_GEOS) -> list[str]:
    """Merge topics across geos, deduped, AM's kept ahead of fallbacks.

    A failed geo is a warning, not an error — one working feed is enough
    to run the show.
    """
    topics: list[str] = []
    seen: set[str] = set()
    for geo in geos:
        try:
            fetched = fetch_topics(geo)
        except Exception as exc:
            print(f"warning: fetch for geo={geo} failed: {exc}", file=sys.stderr)
            continue
        for title in fetched:
            key = title.casefold()
            if key not in seen:
                seen.add(key)
                topics.append(title)
    return topics


if __name__ == "__main__":
    for topic in fetch_all(tuple(sys.argv[1:]) or DEFAULT_GEOS):
        print(topic)
