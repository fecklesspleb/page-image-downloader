# page-image-downloader

**Download all images from a web page with Python**, or from a list of pages, or from a list of direct image URLs. One file, standard library only, no browser and no API keys.

For every page it collects the images the HTML actually points to (`<img src>`, lazy-load attributes, the largest `srcset` candidate, `<picture><source>`, and the `og:image` / `twitter:image` share image), downloads them, checks each one really is an image, skips duplicates by SHA-256, and writes a `manifest.csv` with the size, width and height, alt text and hash of every file. It can also grab linked files such as PDFs or ZIPs.

- Python 3.9 or newer, standard library only.
- Checks robots.txt for every page and every file, sends a `User-Agent` that names the tool, waits 0.5 s between requests to the same host, and backs off on HTTP 429 and 5xx.
- MIT licensed.

## Quick start

```bash
# every image on one page
python3 page_image_downloader.py https://en.wikipedia.org/wiki/Lighthouse

# a list of pages and/or direct image URLs, one per line
python3 page_image_downloader.py -i urls.txt -o images

# skip icons and tracking pixels: nothing under 2 KB or narrower than 100 px
python3 page_image_downloader.py -i urls.txt --min-kb 2 --min-width 100

# images plus every linked PDF
python3 page_image_downloader.py https://www.rfc-editor.org/info/rfc9309 --links pdf
```

Files go to `images/<page host>/<name>-<first 10 hex of sha256>.<ext>`, so two different files called `logo.png` never overwrite each other. The extension comes from the file's bytes, not the URL. A JSON summary is printed at the end; progress lines go to stderr (`-q` hides them).

## What it finds on a page

| In the HTML | What is downloaded |
|---|---|
| `<img src="...">` | `src` |
| `<img srcset="a.jpg 400w, b.jpg 1600w">` | The largest candidate: highest `w`, otherwise highest `x` |
| `<img src="placeholder.gif" data-src="real.jpg">` | `data-src`, `data-lazy-src` or `data-original` before `src`; `data-srcset` / `data-lazy-srcset` before `srcset` |
| `<picture><source srcset="...">` | The largest candidate of each `<source>` |
| `<meta property="og:image">`, `<meta name="twitter:image">` | The share image |
| `<a href="report.pdf">` | Only with `--links pdf` (any comma-separated list of extensions) |

Relative URLs are resolved against `<base href>` and the page URL, `#fragments` are dropped, the same URL is fetched once per page, and `data:` URIs are skipped. A URL whose response is HTML is treated as a page; anything else is treated as a file, so you can mix pages and direct image URLs in one list.

## Checks on every file

- **Is it really an image?** The first bytes are checked for PNG, JPEG, GIF, WebP, AVIF, BMP, ICO and SVG. A `photo.jpg` that comes back as an HTML error page is recorded as `not_an_image` and not saved. A linked `report.pdf` that comes back as a web page (a sign-in wall, say) is recorded as `html_instead_of_file`.
- **Width and height** are read from the file header, without decoding: PNG, JPEG, GIF, WebP (lossy, lossless and extended), AVIF, BMP, and SVG with pixel `width`/`height` (or only a `viewBox`). An SVG sized in `%`, `em` or `ex` has no pixel size, so the columns stay empty.
- **Duplicates**: a file whose SHA-256 was already saved in this run is recorded as `duplicate` and points to the first copy.
- **Filters**: `--min-kb`, `--min-width` and `--max-mb` (default 10). Images whose width can't be read are kept by `--min-width`.

## manifest.csv

| Column | Meaning |
|---|---|
| `page_url` | The URL you gave (the page, or the file itself for direct URLs) |
| `image_url`, `final_url` | The file URL found, and where it ended up after redirects |
| `found_in` | `img`, `img srcset`, `picture source`, `og:image`, `twitter:image`, `a href` or `direct` |
| `alt` | The `<img alt>` text, if any |
| `status` | `saved`, `duplicate`, `too_small`, `too_narrow`, `too_large`, `not_an_image`, `html_instead_of_file`, `robots_disallowed` or `error` |
| `file` | Path inside the output folder (for `duplicate`, the copy already saved) |
| `content_type`, `bytes`, `width`, `height`, `sha256` | What came back |
| `error` | For `error` rows, for example `HTTP 404` |

The summary also reports how many candidates were found on each page and how many were left untried because `--max-per-page` or `--max-files` was reached. The exit code is 1 only when nothing was saved and at least one request failed.

## A real run

On November 3, 2026 (09:41 ET), with `--min-kb 2 --min-width 100`, against `https://www.python.org/`, `https://en.wikipedia.org/wiki/Lighthouse` and one direct SVG on `upload.wikimedia.org`:

| Input | Candidates | Saved | Skipped |
|---|---|---|---|
| python.org home page | 2 (the logo and the `og:image`) | 2 | 0 |
| Wikipedia "Lighthouse" article | 41 | 27 | 7 narrower than 100 px, 4 under 2 KB, 3 blocked by robots.txt |
| Direct SVG URL | 1 | 1 | 0 |

30 files, 1,799,209 bytes, in 19.5 seconds with the default 0.5 s gap between requests to one host (19 of the 30 files came from `thumb.wikimedia.org`). 22 of the 30 came from `srcset`: Wikipedia lists a 2x thumbnail there, and the tool downloads that instead of the smaller `src`. The three robots.txt blocks were all on `en.wikipedia.org`, whose robots.txt disallows `/w/` and `/wiki/Special:` (two MediaWiki footer badges and a `Special:CentralAutoLogin` login-check image). Pillow agreed with the width and height in the manifest for all 25 raster files. Pages change, so your numbers will differ.

## Limits

- No JavaScript. Images that a script adds after the page loads are not found; server-rendered pages, lazy-load attributes and `srcset` work. For those pages you need a headless browser.
- CSS background images (`background-image: url(...)`) and `<video poster>` are not collected.
- No login, cookies or proxies.
- It is a local command-line tool, so it fetches whatever addresses you give it or the page points to, including `localhost` and private network addresses. Don't run it as a public service on untrusted input.
- Malformed URLs in a page don't stop the run: a URL that can't be parsed at all (such as `http://[oops/x.png`) is skipped, and one with a bad port, characters that can't be sent, or a download cut short before its `Content-Length` gets an `error` row in the manifest and is not saved.
- One request at a time. Fine for tens of pages; slow for tens of thousands.
- Already have a list or CSV of image URLs (no page to crawl)? Use [bulk-image-downloader](https://github.com/fecklesspleb/bulk-image-downloader) instead. This tool also accepts direct image URLs mixed with pages; that one is the list/CSV-only path with CSV column detection and parallel downloads.
- robots.txt is checked at the URL you asked for, not again after a redirect. robots.txt that can't be reached (5xx or network error) counts as "disallow everything" for that host, as RFC 9309 suggests.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

37 tests, all offline. Most run against a small local web server (`127.0.0.1`, random port) that serves a made-up gallery page (`tests/fixtures/gallery.html`) with real PNG, GIF, JPEG, WebP and SVG bytes built in the test file. They cover srcset parsing (including commas inside CDN URLs), lazy-load attributes, `<base href>`, image sniffing and header sizes for each format, HTML served under an image name, duplicates by hash, every filter and limit, linked PDFs and sign-in walls, robots.txt (blocked paths are never requested; a missing robots.txt allows, a 5xx disallows), 429 retries, 404 not retried, the per-host delay, malformed URLs and truncated downloads, and the CLI's manifest and exit code.

Last run (Nov 3, 2026, ET): 37 passed on Python 3.9, 3.10, 3.11, 3.12, 3.13 and 3.14 on Linux; pyflakes reports nothing.

## Be a good guest

Download images you have the right to use: your own site, openly licensed media, press kits, or pages whose terms allow it. Being able to download an image doesn't mean you may republish it. Keep the default delay, and use `--ignore-robots` only on sites you run.

## License and disclaimers

MIT. See [LICENSE](LICENSE). This code and README were written with AI assistance. Not affiliated with any of the sites named.

To run this at scale without your own machine, I (fecklesspleb) also maintain two hosted Apify Actors: [Bulk Image and File Downloader](https://apify.com/fecklesspleb/bulk-image-downloader) ($1.00 per 1,000 files saved) and [Website Screenshot and PDF](https://apify.com/fecklesspleb/website-screenshot) ($0.80 per 1,000 screenshots), each plus a run-start fee of $0.00005 per GB of memory, charged at least once per run.
