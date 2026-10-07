"""gallery-dl routing and jobs."""

import asyncio
from pathlib import Path

import pytest

import app.bot.telegram_bot as tb
from app.services import gallery, user_settings
from tests.fakes import callback_update, make_context, text_update


def test_known_and_unknown_sites():
    assert gallery.gallery_category("https://www.pixiv.net/en/artworks/12345678") == "pixiv"
    assert gallery.gallery_category("https://danbooru.donmai.us/posts/1") == "danbooru"
    assert gallery.gallery_category("https://example.com/page") is None


def test_site_labels():
    assert gallery.site_label("directlink") == "File link"
    assert gallery.site_label("pixiv") == "Pixiv"


def test_output_lines():
    assert gallery.parse_output_line("/srv/Download/Gallery/pixiv/1.jpg\n") == Path(
        "/srv/Download/Gallery/pixiv/1.jpg"
    )
    assert gallery.parse_output_line("# /srv/Download/Gallery/pixiv/1.jpg") == Path(
        "/srv/Download/Gallery/pixiv/1.jpg"
    )
    assert gallery.parse_output_line("[pixiv][info] something") is None
    assert gallery.parse_output_line("relative/path.jpg") is None


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "Download"
    root.mkdir()
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", root)
    monkeypatch.setattr(tb, "GALLERY_DIR", root / "Gallery")
    monkeypatch.setattr(user_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(tb, "download_jobs", {})
    monkeypatch.setattr(tb, "link_requests", {})
    monkeypatch.setattr(tb, "search_ui", None)
    return root


async def _drain():
    while tb.background_tasks:
        await asyncio.gather(*list(tb.background_tasks), return_exceptions=True)
        await asyncio.sleep(0)


async def test_unhandled_gallery_link_is_offered_to_gallery_dl(env, monkeypatch):
    async def fake_download(url, destination, *, on_file=None, set_process=None, **kwargs):
        target = destination / "pixiv" / "1_p0.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"png")
        on_file(target)
        return [target]

    monkeypatch.setattr(tb, "download_gallery", fake_download)
    context = make_context()

    await tb.on_text(text_update(context, "https://www.pixiv.net/en/artworks/12345678"), context)
    prompt = context.bot.called("reply_text")[-1]
    assert "<b>Pixiv</b> link" in prompt.kwargs["text"]
    go = next(
        b.callback_data
        for row in prompt.kwargs["reply_markup"].inline_keyboard
        for b in row
        if "Download" in b.text
    )

    await tb.handle_link_request_callback(callback_update(context, go), context)
    await _drain()

    job = next(iter(tb.download_jobs.values()))
    assert job["provider"] == "gallery-dl" and job["status"] == "completed"
    assert tb.job_outputs(job) == ["Gallery/pixiv/1_p0.png"]
    final = context.bot.called("send_message")[-1].kwargs
    assert final["text"].startswith("✅") and "Gallery · Pixiv" in final["text"]


async def test_gallery_failure_offers_retry(env, monkeypatch):
    async def failing(url, destination, **kwargs):
        raise RuntimeError("gallery-dl found nothing to download at this link.")

    monkeypatch.setattr(tb, "download_gallery", failing)
    context = make_context()

    job = await tb.start_gallery_download(
        context.application, 1, "https://imgur.com/a/x", "imgur", 1
    )
    await _drain()

    assert job["status"] == "failed"
    final = context.bot.called("send_message")[-1].kwargs
    assert "found nothing to download" in final["text"]
    assert "job:retry:" in str(final["reply_markup"].inline_keyboard)
