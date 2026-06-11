from app.services.rss import parse_feed


def test_parse_rss_feed():
    entries = parse_feed("""
        <rss version="2.0">
          <channel>
            <item>
              <title>Episode 1</title>
              <guid>episode-1</guid>
              <link>https://example.test/episode-1.torrent</link>
              <pubDate>Thu, 11 Jun 2026 12:00:00 GMT</pubDate>
            </item>
          </channel>
        </rss>
        """)

    assert len(entries) == 1
    assert entries[0].id == "episode-1"
    assert entries[0].url == "https://example.test/episode-1.torrent"
    assert entries[0].published_at is not None


def test_parse_atom_feed():
    entries = parse_feed("""
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>Release</title>
            <id>tag:example.test,2026:release</id>
            <updated>2026-06-11T12:00:00Z</updated>
            <link href="https://example.test/release.magnet" />
          </entry>
        </feed>
        """)

    assert len(entries) == 1
    assert entries[0].title == "Release"
    assert entries[0].url == "https://example.test/release.magnet"
