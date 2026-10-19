# audio-core (public payload)

The ffmpeg.wasm engine for DataBridge's Audio Converter.

The extension's base folder is kept small: this ~31 MB `ffmpeg-core.wasm` is
**not** shipped inside it. Instead the extension downloads this file on demand
(Settings → 🧩 Add-ons → ⬇ Download, or the button on the converter page),
verifies its sha256 and stores it in the browser's IndexedDB. Removing the
add-on deletes it again; nothing else changes.

- URL used by the extension:
  `https://raw.githubusercontent.com/alif13579/databridge-releases/main/audio-core/ffmpeg-core.wasm`
- sha256: `9f57947a5bd530d8f00c5b3f2cb2a3492faa7e5d823315342d6a8656d0a6b7b7`
- Version: ffmpeg-core 0.12.9 (single-thread UMD build)

⚠️ If this file is ever replaced, update the `sha256` (and `bytes`) in
`DataBridge-Extension/addons.js` — every install verifies the checksum, and a
mismatch is rejected. The extension bundles only `ffmpeg.js`, `814.ffmpeg.js`
and `ffmpeg-core.js` (~120 KB total) from the `lib/ffmpeg/` folder.
