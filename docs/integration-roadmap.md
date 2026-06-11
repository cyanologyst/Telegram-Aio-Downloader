# Integration Roadmap

This document turns the GitHub integration review into implementation guidance
for this repository.

## Implemented Boundaries

The project now exposes thin integration boundaries instead of vendoring code
from large third-party downloader bots:

- `app.downloaders.hentai.HanimePluginDownloader` delegates supported hentai
  sites to `yt-dlp` plus `hanime-plugin`.
- `app.downloaders.gallery.GalleryDlDownloader` delegates image and gallery
  sites to `gallery-dl`.
- `app.downloaders.jdownloader.JDownloaderProvider` delegates complex hoster
  and container links to a configured HTTP bridge for JDownloader.
- `app.services.cloud_mirror.RcloneMirrorService` mirrors completed downloads
  to any configured `rclone` remote.
- `app.services.rss.FeedReader` parses RSS/Atom feeds for future automated
  download jobs.
- `app.services.file_links.SignedFileLinkService` creates temporary signed
  links for serving files from the VPS.

These are designed as clean wrappers around installed tools and services. That
keeps licensing simple and avoids copying GPL-licensed bot code directly into
this project.

## Deployment Notes

Install the optional tools you want to enable:

```bash
pip install gallery-dl hanime-plugin
sudo apt-get install rclone
```

`hanime.tv` support may require Deno depending on the current extractor path in
`hanime-plugin`.

For JDownloader, run JDownloader on the VPS and expose a small local bridge
service that accepts `POST /downloads` with this payload:

```json
{
  "url": "https://example.test/file.dlc",
  "destination": "/srv/downloads",
  "options": {}
}
```

The bridge should return JSON with optional `task_id`, `package_id`, `status`,
`message`, and `title` fields.

## Torrent Search Policy

Torrent search remains intentionally limited to the TPB/API Bay crawler. Other
torrent search integrations were removed to keep the bot lightweight and aligned
with the project's current scope.

## Next Work

- Wire `build_downloader_registry()` into the Telegram handler composition
  layer as the legacy runtime is split further.
- Add queue persistence for RSS-triggered jobs.
- Add mini-app controls for cloud mirror destinations and signed file links.
- Add an integration test environment with mocked `gallery-dl`, `yt-dlp`,
  `rclone`, and JDownloader bridge processes.
