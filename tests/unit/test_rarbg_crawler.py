import pytest

from app.downloaders.torrents.rarbg import RARBGCrawler, RARBGVerificationError

SEARCH_HTML = """
<html><body>
<table class="lista2t">
<tr class="lista2">
  <td class="lista"></td>
  <td class="lista">
    <a href="/torrent/ubuntu-server-22-04-5660060.html" title="Ubuntu Server 22.04">Ubuntu Server 22.04</a>
  </td>
  <td class="lista"><a href="/other/">Other</a><a href="/other/tutorials/">/Tutorials</a></td>
  <td class="lista">2023-05-17 10:01:03</td>
  <td class="lista">996.6 MB</td>
  <td class="lista"><font color="#990000">43</font></td>
  <td class="lista">6</td>
  <td class="lista">xHOBBiTx</td>
</tr>
</table>
</body></html>
"""


DETAIL_HTML = """
<html><body>
<h1 class="black">Ubuntu Server 22.04</h1>
<table class="lista">
<tr><td>Size:</td><td>996.6 MB</td></tr>
<tr><td>Added:</td><td>2023-05-17 10:01:03</td></tr>
<tr><td>Category:</td><td>Other/Tutorials</td></tr>
<tr><td>Peers:</td><td>Seeders : 43 , Leechers : 6</td></tr>
<tr><td>Torrent:</td><td><a href="magnet:?xt=urn:btih:ABC&dn=Ubuntu">Ubuntu</a></td></tr>
</table>
</body></html>
"""


def test_parse_rarbg_search_results():
    crawler = RARBGCrawler("https://rargb.to")

    results = crawler._parse_results(SEARCH_HTML)

    assert len(results) == 1
    result = results[0].to_dict()
    assert result["id"] == "torrent/ubuntu-server-22-04-5660060.html"
    assert result["name"] == "Ubuntu Server 22.04"
    assert result["size"] == "996.6 MB"
    assert result["seeders"] == "43"
    assert result["leechers"] == "6"


def test_parse_rarbg_detail_extracts_magnet_and_peers():
    crawler = RARBGCrawler("https://rargb.to")

    detail = crawler._parse_detail(DETAIL_HTML, "torrent/ubuntu-server-22-04-5660060.html")

    assert detail is not None
    data = detail.to_dict()
    assert data["magnet"].startswith("magnet:?xt=urn:btih:ABC")
    assert data["seeders"] == "43"
    assert data["leechers"] == "6"


def test_rarbg_verification_without_content_raises():
    html = "<html><title>Checking your browser</title><body>captcha required</body></html>"

    with pytest.raises(RARBGVerificationError):
        RARBGCrawler._raise_if_verification(html, "https://rargb.to")


def test_rarbg_challenge_script_with_content_is_allowed():
    html = '<html><a href="/torrent/example.html">Example</a><script src="/cdn-cgi/challenge-platform/x.js"></script></html>'

    RARBGCrawler._raise_if_verification(html, "https://rargb.to")
