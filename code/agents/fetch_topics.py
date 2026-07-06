"""Fetch trending topics for word research.

Each source is one small function returning raw titles; fetch_all() merges
and dedupes them. A failed source is a warning, not an error — one working
feed is enough to run the show. Titles come back raw and messy (lowercase
names, mixed languages, emoji) — that's fine, the trend_research skill's
job is to extract the guessable core concept from noisy input.

Sources:
- Google Trends RSS       what people SEARCH  (free, no key)
- Wikipedia top reads     what people READ    (Wikimedia API, free, no key;
                          hy edition = a specifically Armenian signal)
- YouTube trending in AM  what people WATCH   (reuses the scorer's OAuth)
"""
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent

TRENDS_RSS = "https://trends.google.com/trending/rss?geo={geo}"
WIKI_TOP = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/"
            "top/{lang}.wikipedia/all-access/{y}/{m:02d}/{d:02d}")
# Wikimedia rejects generic UAs; Google Trends rejects urllib's default.
USER_AGENT = "alias-game/0.1 (https://github.com/gregaro/alias-game)"

# AM first (our audience), US as a broader fallback — Armenia's daily list
# can be short and dominated by one news story.
TREND_GEOS = ("AM", "US")


def _get(url: str, timeout: int = 10) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_google_trends(geos=TREND_GEOS) -> list[str]:
    """Trending search titles across country codes."""
    titles = []
    for geo in geos:
        root = ET.fromstring(_get(TRENDS_RSS.format(geo=geo)))
        titles += [t.strip() for item in root.iter("item")
                   if (t := item.findtext("title", ""))]
    return titles


def fetch_wikipedia(lang: str = "hy", limit: int = 15) -> list[str]:
    """Yesterday's most-read articles. Article titles are cleaner concepts
    than search queries ("Վարդավառ", not "when is vardavar 2026")."""
    for days_back in (1, 2):  # yesterday's data can lag; fall back one day
        day = datetime.now(timezone.utc) - timedelta(days=days_back)
        url = WIKI_TOP.format(lang=lang, y=day.year, m=day.month, d=day.day)
        try:
            data = json.loads(_get(url))
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 404 or days_back == 2:
                raise
    articles = data["items"][0]["articles"]
    titles = []
    for entry in articles:
        title = entry["article"].replace("_", " ")
        # ":" filters non-article namespaces (Սպասարկող:, Կատեգորիա:, ...);
        # the main page tops every day's list and means nothing.
        if ":" in title or title in ("Գլխավոր էջ", "Main Page"):
            continue
        titles.append(title)
        if len(titles) == limit:
            break
    return titles


def fetch_youtube_trending(region: str = "AM", limit: int = 15) -> list[str]:
    """Trending video titles — closest signal to our actual audience."""
    sys.path.insert(0, str(HERE.parent / "scorer"))
    from youtube_auth import get_service  # reuses cached token in ../secrets
    resp = get_service().videos().list(
        part="snippet", chart="mostPopular", regionCode=region,
        maxResults=limit,
    ).execute()
    return [item["snippet"]["title"] for item in resp.get("items", [])]


SOURCES = (
    ("google-trends", fetch_google_trends),
    ("wikipedia-hy", fetch_wikipedia),
    ("youtube-am", fetch_youtube_trending),
)


def fetch_all() -> list[str]:
    """All sources merged, deduped, in SOURCES order."""
    topics: list[str] = []
    seen: set[str] = set()
    for name, fetch in SOURCES:
        try:
            fetched = fetch()
        except Exception as exc:
            print(f"warning: {name} fetch failed: {exc}", file=sys.stderr)
            continue
        for title in fetched:
            key = title.casefold()
            if key not in seen:
                seen.add(key)
                topics.append(title)
    return topics


if __name__ == "__main__":
    # Standalone: print topics tagged by source, to eyeball each feed.
    for name, fetch in SOURCES:
        try:
            for title in fetch():
                print(f"[{name}] {title}")
        except Exception as exc:
            print(f"warning: {name} fetch failed: {exc}", file=sys.stderr)
