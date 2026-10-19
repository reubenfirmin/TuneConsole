# TuneConsole logo

The source is [`favicon.svg`](../../src/yt_playlist/web/static/favicon.svg): a flat TC monogram
on a blue tile. It uses the application's blue (`#7aa2f7`) and ink (`#0b0920`), with no gradients,
glow, or texture. The two paths can use one colour when the mark is needed without a tile.

The same SVG is mirrored at `../tuneconsole-site/public/icon.svg`. The app header, browser
favicon, website header, and connection diagram use the vector directly. PNG exports serve
the browser extension, Flatpak, macOS, and the website's existing `/icon.png` URL.

Regenerate the PNGs with ImageMagick from the repository root:

```sh
magick -background none -density 1536 src/yt_playlist/web/static/favicon.svg -resize 1024x1024 PNG32:packaging/macos/TuneConsole.png
magick -background none -density 384 src/yt_playlist/web/static/favicon.svg -resize 256x256 PNG32:packaging/flatpak/com.tuneconsole.TuneConsole.png
magick -background none -density 384 src/yt_playlist/web/static/favicon.svg -resize 256x256 PNG32:../tuneconsole-site/public/icon.png
magick -background none -density 384 src/yt_playlist/web/static/favicon.svg -resize 128x128 PNG32:extension/icons/icon128.png
magick -background none -density 384 src/yt_playlist/web/static/favicon.svg -resize 48x48 PNG32:extension/icons/icon48.png
magick -background none -density 384 src/yt_playlist/web/static/favicon.svg -resize 32x32 PNG32:extension/icons/icon32.png
magick -background none -density 384 src/yt_playlist/web/static/favicon.svg -resize 16x16 PNG32:extension/icons/icon16.png
```
