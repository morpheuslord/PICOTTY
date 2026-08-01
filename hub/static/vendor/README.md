# hub/static/vendor — optional vendored front-end libraries

These libraries are **progressive enhancements**. The dashboard works fully
without them; when present it upgrades two surfaces:

| File(s)                                          | Library            | Version | Enables |
|--------------------------------------------------|--------------------|---------|---------|
| `xterm.js`, `xterm.css`                          | xterm.js           | 5.3.0   | Real terminal emulator for the serial console (phase 3) |
| `xterm-addon-fit.js`                             | xterm addon-fit    | 0.8.0   | Auto-sizes the terminal to its container |
| `asciinema-player.min.js`, `asciinema-player.css`| asciinema-player   | 3.8.0   | Inline session replay of `*.cast` recordings (phase 7) |

## Why they aren't committed

The hub runs on an **isolated VLAN with no internet**, so:

- We never fetch these at runtime (no CDN calls). `index.html` references the
  local `vendor/<file>` paths only.
- We don't commit the minified blobs to the repo.

`index.html` loads these files with plain `<script>` / `<link>` tags. Until you
vendor them they simply **404 harmlessly** — the browser logs a load error and
moves on. `app.js` feature-detects the globals:

- `window.Terminal` (xterm) — absent → the built-in DOM log renderer is used,
  exactly as before.
- `window.AsciinemaPlayer` — absent → the Replay dialog offers a `.cast`
  download link plus an `asciinema play` hint instead of an inline player.

## How to vendor them

On a machine **with** internet:

```sh
cd hub/static/vendor
sh fetch-vendor.sh
```

That downloads the pinned files (via jsDelivr, as a download mirror only) into
this directory. Then copy the whole `hub/static/vendor/` directory onto the hub.
No other wiring is needed — the filenames are already referenced by `index.html`.

Bump versions deliberately in `fetch-vendor.sh`, but keep the output **filenames**
stable, because `index.html` references these exact names.
