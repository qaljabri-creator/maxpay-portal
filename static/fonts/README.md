# Fonts

Cairo, self-hosted.

The client portal ships under `default-src 'self'` (spec §11), so a webfont
either comes from this origin or it does not load at all. These two files are
the Google Fonts build of Cairo, split the way that build splits it:

| File                 | Subset             | Weights   |
| -------------------- | ------------------ | --------- |
| `cairo-arabic.woff2` | Arabic + presentation forms | 400-700 variable |
| `cairo-latin.woff2`  | Latin basic        | 400-700 variable |

Both are declared in `static/css/embed.css` with the `unicode-range` each
subset covers, so a screen of Arabic never pulls the Latin file and a wallet
number never pulls more than it needs.

One variable face per subset rather than four static cuts: the flow uses 400,
600 and 700, and three static files would cost more than the one variable
file does.

Licence: SIL Open Font License 1.1, bundled here as `OFL.txt` as the licence
requires. Copyright 2009 The Cairo Project Authors.
Upstream: https://github.com/Gue3bara/Cairo

## Updating

Ask Google Fonts for the CSS with a browser `User-Agent` (any other UA gets
TTF instead of woff2), then download the URLs it names:

    https://fonts.googleapis.com/css2?family=Cairo:wght@400..700&display=swap

Keep the filenames above, and copy the fresh `unicode-range` values into
`embed.css` alongside them.
