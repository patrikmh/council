"""news.py feed parsing: RSS 2.0 and Atom."""

from app.news import SOURCES, Source, _clean, _parse_feed

SRC = Source("test", "Test", "https://x/feed", "neutral", paywalled=False)

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Saab s&#228;ljer &lt;b&gt;Globaleye&lt;/b&gt;</title>
    <description>Nato k&#246;per plan.</description>
    <link> https://x.se/1 </link>
    <pubDate>Tue, 07 Jul 2026 08:00:00 +0200</pubDate>
  </item>
  <item><title></title><description>no title, skipped</description></item>
  <item><title>Second</title></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Ekot: nyhet</title>
    <summary>Sammanfattning</summary>
    <link href="https://sr.se/1"/>
    <updated>2026-07-07T08:00:00Z</updated>
  </entry>
</feed>"""


def test_rss_items_cleaned_and_skipped():
    items = _parse_feed(RSS, SRC)
    assert len(items) == 2  # titleless item skipped
    first = items[0]
    assert first["title"] == "Saab säljer Globaleye"  # entities + tags cleaned
    assert first["summary"] == "Nato köper plan."
    assert first["link"] == "https://x.se/1"
    assert items[1]["summary"] == ""


def test_atom_feed_parsed():
    items = _parse_feed(ATOM, SRC)
    assert items == [{
        "source": "test",
        "title": "Ekot: nyhet",
        "summary": "Sammanfattning",
        "link": "https://sr.se/1",
        "published": "2026-07-07T08:00:00Z",
    }]


def test_clean_handles_none_and_nested_tags():
    assert _clean(None) == ""
    assert _clean("<p>a <b>b</b> c</p>") == "a b c"


def test_source_ids_are_unique():
    ids = [s.id for s in SOURCES]
    assert len(ids) == len(set(ids))
