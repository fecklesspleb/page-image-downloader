#!/usr/bin/env python3
"""Download every image on a web page, or on a list of pages, with Python.

Give it page URLs, direct image URLs, or a mix. For each page it collects the
images the HTML points to:

* ``<img src>`` and lazy-load attributes (``data-src``, ``data-lazy-src``,
  ``data-original``)
* ``srcset`` on ``<img>`` and ``<picture><source>``, at the largest size offered
* ``og:image`` and ``twitter:image`` meta tags

and, with ``--links pdf,zip``, files linked from ``<a href>`` with those
extensions. Every file is checked by its first bytes (an HTML error page is not
saved as ``photo.jpg``), de-duplicated by SHA-256, and listed in
``manifest.csv`` with its size, width and height, alt text and hash.

robots.txt is checked for every page and file. Standard library only. MIT
licensed. https://github.com/fecklesspleb/page-image-downloader
"""

import argparse
import csv
import hashlib
import html.parser
import http.client
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

__version__ = "1.0.0"

USER_AGENT = "page-image-downloader/%s (+https://github.com/fecklesspleb/page-image-downloader)" % __version__
ROBOTS_AGENT = "page-image-downloader"

MAX_PAGE_BYTES = 5 * 1024 * 1024

MANIFEST_FIELDS = [
    "page_url", "image_url", "final_url", "found_in", "alt", "status",
    "file", "content_type", "bytes", "width", "height", "sha256", "error",
]

LAZY_SRC_ATTRS = ("data-src", "data-lazy-src", "data-original")
LAZY_SRCSET_ATTRS = ("data-srcset", "data-lazy-srcset")
META_IMAGE_KEYS = ("og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src")

EXT_BY_TYPE = {
    "png": ".png", "jpeg": ".jpg", "gif": ".gif", "webp": ".webp",
    "svg": ".svg", "avif": ".avif", "bmp": ".bmp", "ico": ".ico",
}
TYPE_BY_MIME = {
    "image/png": "png", "image/jpeg": "jpeg", "image/jpg": "jpeg", "image/pjpeg": "jpeg",
    "image/gif": "gif", "image/webp": "webp", "image/svg+xml": "svg", "image/avif": "avif",
    "image/bmp": "bmp", "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico",
}


# --------------------------------------------------------------------------
# Finding images in HTML


def parse_srcset(value):
    """Split a srcset value into ``(url, descriptor)`` pairs.

    Follows the HTML spec's approach: a URL runs until whitespace, a trailing
    comma ends a candidate, and a descriptor runs until the next comma. This
    keeps commas inside URLs (common on image CDNs) intact.
    """
    out = []
    s = value or ""
    i, n = 0, len(s)
    while i < n:
        while i < n and (s[i].isspace() or s[i] == ","):
            i += 1
        if i >= n:
            break
        start = i
        while i < n and not s[i].isspace():
            i += 1
        url = s[start:i]
        descriptor = ""
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            start = i
            while i < n and s[i] != ",":
                i += 1
            descriptor = s[start:i].strip()
        if url:
            out.append((url, descriptor))
    return out


def best_srcset_url(value):
    """Return the largest candidate in a srcset: highest ``w``, else highest ``x``."""
    best, best_key = None, None
    for url, desc in parse_srcset(value):
        key = (0, 1.0)
        m = re.match(r"^(\d+)w$", desc)
        if m:
            key = (1, float(m.group(1)))
        else:
            m = re.match(r"^(\d+(?:\.\d+)?)x$", desc)
            if m:
                key = (0, float(m.group(1)))
        if best_key is None or key > best_key:
            best, best_key = url, key
    return best


class ImageFinder(html.parser.HTMLParser):
    """Collect image and file candidates from one HTML document, in page order."""

    def __init__(self, link_extensions=()):
        super().__init__(convert_charrefs=True)
        self.base = None
        self.found = []          # list of (raw_url, found_in, alt)
        self.link_extensions = tuple("." + e.lower().lstrip(".") for e in link_extensions)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "base" and a.get("href") and self.base is None:
            self.base = a["href"].strip()
        elif tag == "img":
            alt = a.get("alt", "").strip()
            srcset = next((a[k] for k in LAZY_SRCSET_ATTRS if a.get(k)), "") or a.get("srcset", "")
            best = best_srcset_url(srcset) if srcset else None
            if best:
                self.found.append((best, "img srcset", alt))
            else:
                src = next((a[k] for k in LAZY_SRC_ATTRS if a.get(k)), "") or a.get("src", "")
                if src:
                    self.found.append((src, "img", alt))
        elif tag == "source":
            srcset = next((a[k] for k in LAZY_SRCSET_ATTRS if a.get(k)), "") or a.get("srcset", "")
            best = best_srcset_url(srcset) if srcset else None
            if best and (not a.get("type") or a["type"].lower().startswith("image/")):
                self.found.append((best, "picture source", ""))
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in META_IMAGE_KEYS and a.get("content"):
                self.found.append((a["content"], key, ""))
        elif tag == "a" and self.link_extensions and a.get("href"):
            try:
                path = urllib.parse.urlsplit(a["href"].strip()).path.lower()
            except ValueError:  # e.g. "http://[oops/x.pdf"
                return
            if path.endswith(self.link_extensions):
                self.found.append((a["href"], "a href", ""))


def find_images(page_html, page_url, link_extensions=()):
    """Return a de-duplicated list of dicts ``{url, found_in, alt}`` for one page.

    Relative URLs are resolved against ``<base href>`` (if any) and the page URL.
    ``data:`` URIs and non-HTTP schemes are dropped.
    """
    finder = ImageFinder(link_extensions)
    finder.feed(page_html)
    finder.close()
    base = page_url
    if finder.base:
        try:
            base = urllib.parse.urljoin(page_url, finder.base)
        except ValueError:  # a malformed <base href> is ignored
            pass
    seen, out = set(), []
    for raw, found_in, alt in finder.found:
        raw = raw.strip()
        if not raw or raw.lower().startswith(("data:", "javascript:", "blob:")):
            continue
        try:
            url = urllib.parse.urldefrag(urllib.parse.urljoin(base, raw))[0]
            if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
                continue
        except ValueError:  # e.g. "http://[oops/x.png" (Invalid IPv6 URL)
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "found_in": found_in, "alt": alt})
    return out


# --------------------------------------------------------------------------
# Recognising image files from their bytes


def sniff_type(data):
    """Return a short image type name from the file's first bytes, or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return "avif"
    if data.startswith(b"BM") and len(data) > 26:
        return "bmp"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "ico"
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head) or \
            (head.startswith(b"<!doctype svg")):
        return "svg"
    return None


def _svg_length(value):
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(px)?\s*$", value or "")
    return int(float(m.group(1))) if m else None


def image_size(data, kind=None):
    """Return ``(width, height)`` read from the file header, or ``(None, None)``.

    PNG, JPEG, GIF, WebP (lossy, lossless, extended), AVIF, BMP, and SVG with
    pixel ``width``/``height``, or a ``viewBox`` when it has neither. An SVG
    sized in other units (``%``, ``em``, ``ex``) has no pixel size. Nothing is
    decoded.
    """
    kind = kind or sniff_type(data)
    try:
        if kind == "png" and len(data) >= 24 and data[12:16] == b"IHDR":
            return struct.unpack(">II", data[16:24])
        if kind == "gif" and len(data) >= 10:
            return struct.unpack("<HH", data[6:10])
        if kind == "bmp":
            w, h = struct.unpack("<ii", data[18:26])
            return w, abs(h)
        if kind == "jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker == 0xFF:
                    i += 1
                    continue
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + seg_len
            return None, None
        if kind == "webp" and len(data) >= 25:
            chunk = data[12:16]
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                b = data[21:25]
                bits = b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":
                w = data[24] | (data[25] << 8) | (data[26] << 16)
                h = data[27] | (data[28] << 8) | (data[29] << 16)
                return w + 1, h + 1
        if kind == "avif":
            i = data.find(b"ispe")
            if i != -1 and len(data) >= i + 16:
                return struct.unpack(">II", data[i + 8:i + 16])
        if kind == "svg":
            m = re.search(rb"<svg\b[^>]*>", data[:8192], re.I | re.S)
            if m:
                tag = m.group(0).decode("utf-8", "replace")
                attrs = {}
                for k, _q, v in re.findall(r'([\w:-]+)\s*=\s*(["\'])(.*?)\2', tag, re.S):
                    attrs[k.lower()] = v
                w, h = _svg_length(attrs.get("width")), _svg_length(attrs.get("height"))
                if w and h:
                    return w, h
                if attrs.get("width") or attrs.get("height"):
                    # "100%", "13.156ex", "2em": no pixel size to report.
                    return None, None
                vb = re.split(r"[\s,]+", attrs.get("viewbox", "").strip())
                if len(vb) == 4:
                    return int(float(vb[2])), int(float(vb[3]))
    except (struct.error, IndexError, ValueError):
        pass
    return None, None


# --------------------------------------------------------------------------
# HTTP


class Fetcher:
    """Polite HTTP: one User-Agent, a minimum gap per host, retries on 429/5xx."""

    def __init__(self, delay=0.5, timeout=30, retries=3, opener=None, sleep=time.sleep, clock=time.monotonic):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.opener = opener or urllib.request.build_opener()
        self.sleep = sleep
        self.clock = clock
        self._last = {}

    def _wait_for_host(self, url):
        host = urllib.parse.urlsplit(url).netloc.lower()
        last = self._last.get(host)
        if last is not None:
            gap = self.delay - (self.clock() - last)
            if gap > 0:
                self.sleep(gap)
        self._last[host] = self.clock()

    def get(self, url, max_bytes):
        """Return ``(final_url, content_type, body, too_big)``.

        Raises HTTPError/URLError/OSError, ``http.client.HTTPException`` (bad
        URL, or ``IncompleteRead`` when the body is shorter than its
        Content-Length) or ValueError.
        """
        attempt = 0
        while True:
            self._wait_for_host(url)
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            try:
                with self.opener.open(req, timeout=self.timeout) as resp:
                    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    length = resp.headers.get("Content-Length")
                    if length and length.isdigit() and int(length) > max_bytes:
                        return resp.geturl(), ctype, b"", True
                    body = resp.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        return resp.geturl(), ctype, b"", True
                    if length and length.isdigit() and len(body) < int(length):
                        # The connection closed early: never save half a file.
                        raise http.client.IncompleteRead(body, int(length) - len(body))
                    return resp.geturl(), ctype, body, False
            except urllib.error.HTTPError as e:
                if (e.code == 429 or 500 <= e.code < 600) and attempt < self.retries:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                    self.sleep(min(wait, 60))
                    attempt += 1
                    continue
                raise


class Robots:
    """robots.txt per origin (RFC 9309): 4xx means allowed, 5xx or unreachable means not."""

    def __init__(self, fetcher, agent=ROBOTS_AGENT):
        self.fetcher = fetcher
        self.agent = agent
        self._cache = {}

    def allowed(self, url):
        parts = urllib.parse.urlsplit(url)
        origin = "%s://%s" % (parts.scheme, parts.netloc.lower())
        if origin not in self._cache:
            rp = urllib.robotparser.RobotFileParser()
            try:
                _final, _ctype, body, _big = self.fetcher.get(origin + "/robots.txt", 512 * 1024)
                rp.parse(body.decode("utf-8", "replace").splitlines())
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500:
                    rp.allow_all = True
                else:
                    rp.disallow_all = True
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
                rp.disallow_all = True
            self._cache[origin] = rp
        return self._cache[origin].can_fetch(self.agent, url)


# --------------------------------------------------------------------------
# Saving


def safe_name(url, sha, kind_ext):
    """``<last path part, cleaned>-<first 10 hex of sha256><ext>``."""
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    stem = os.path.basename(path.rstrip("/")) or "file"
    stem, ext = os.path.splitext(stem)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")[:60] or "file"
    ext = kind_ext or (ext.lower() if re.match(r"^\.[a-z0-9]{1,5}$", ext.lower()) else ".bin")
    return "%s-%s%s" % (stem, sha[:10], ext)


class Downloader:
    def __init__(self, out_dir, fetcher, robots=None, min_bytes=0, min_width=0, max_bytes=10 * 1024 * 1024,
                 max_per_page=0, max_files=0, link_extensions=(), log=None):
        self.out_dir = out_dir
        self.fetcher = fetcher
        self.robots = robots
        self.min_bytes = min_bytes
        self.min_width = min_width
        self.max_bytes = max_bytes
        self.max_per_page = max_per_page
        self.max_files = max_files
        self.link_extensions = tuple(e.lower().lstrip(".") for e in link_extensions)
        self.log = log or (lambda msg: None)
        self.rows = []
        self.by_sha = {}
        self.saved = 0
        self.pages = []

    def _row(self, **kw):
        row = dict.fromkeys(MANIFEST_FIELDS, "")
        row.update(kw)
        self.rows.append(row)
        return row

    def _allowed(self, url):
        return self.robots is None or self.robots.allowed(url)

    def _url_problem(self, url):
        """Return an error text for a URL that can't be requested, else None."""
        try:
            parts = urllib.parse.urlsplit(url)
            parts.port  # raises ValueError for "http://host:abc/"
        except ValueError as e:
            return "invalid URL: %s" % e
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return "invalid URL: no http(s) host"
        return None

    def run(self, urls):
        for url in urls:
            if self.max_files and self.saved >= self.max_files:
                break
            self.process(url)
        return self.rows

    def process(self, url):
        """A URL that returns HTML is a page; anything else is treated as a file."""
        problem = self._url_problem(url)
        if problem:
            self._row(page_url=url, image_url=url, status="error", error=problem)
            self.log("error %s: %s" % (url, problem))
            return
        if not self._allowed(url):
            self._row(page_url=url, image_url=url, status="robots_disallowed")
            self.log("robots.txt disallows %s" % url)
            return
        try:
            final, ctype, body, too_big = self.fetcher.get(url, max(self.max_bytes, MAX_PAGE_BYTES))
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            self._row(page_url=url, image_url=url, status="error", error=_err(e))
            self.log("error %s: %s" % (url, _err(e)))
            return
        if ctype in ("text/html", "application/xhtml+xml") and not too_big:
            self.process_page(url, final, body)
        else:
            # The first request allowed room for a page; apply the file limit now.
            too_big = too_big or len(body) > self.max_bytes
            self.save(url, url, {"found_in": "direct", "alt": ""}, final, ctype, body, too_big)

    def process_page(self, url, final, body):
        text = body.decode(_charset(body), "replace")
        found = find_images(text, final, self.link_extensions)
        page = {"page_url": url, "candidates": len(found), "not_tried": 0}
        self.pages.append(page)
        self.log("page %s: %d candidate(s)" % (url, len(found)))
        saved_here = 0
        for n, item in enumerate(found):
            if (self.max_files and self.saved >= self.max_files) or \
                    (self.max_per_page and saved_here >= self.max_per_page):
                page["not_tried"] = len(found) - n
                self.log("page %s: limit reached, %d candidate(s) not tried" % (url, page["not_tried"]))
                break
            problem = self._url_problem(item["url"])
            if problem:
                self._row(page_url=url, image_url=item["url"], found_in=item["found_in"], alt=item["alt"],
                          status="error", error=problem)
                continue
            if not self._allowed(item["url"]):
                self._row(page_url=url, image_url=item["url"], found_in=item["found_in"], alt=item["alt"],
                          status="robots_disallowed")
                continue
            try:
                f_final, ctype, data, too_big = self.fetcher.get(item["url"], self.max_bytes)
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
                self._row(page_url=url, image_url=item["url"], found_in=item["found_in"], alt=item["alt"],
                          status="error", error=_err(e))
                continue
            if self.save(url, item["url"], item, f_final, ctype, data, too_big) == "saved":
                saved_here += 1

    def save(self, page_url, url, item, final, ctype, data, too_big):
        base = dict(page_url=page_url, image_url=url, final_url=final, found_in=item["found_in"],
                    alt=item["alt"], content_type=ctype)
        if too_big:
            self._row(status="too_large", **base)
            return "too_large"
        kind = sniff_type(data) or TYPE_BY_MIME.get(ctype)
        is_link = item["found_in"] == "a href"
        if not kind and not is_link:
            self._row(status="not_an_image", bytes=len(data), **base)
            return "not_an_image"
        if is_link and not kind and (ctype in ("text/html", "application/xhtml+xml") or
                                     data[:512].lstrip().lower().startswith((b"<!doctype html", b"<html"))):
            # A linked "report.pdf" that answers with a web page (a login wall, a 200 error page).
            self._row(status="html_instead_of_file", bytes=len(data), **base)
            return "html_instead_of_file"
        width, height = image_size(data, kind) if kind else (None, None)
        sha = hashlib.sha256(data).hexdigest()
        base.update(bytes=len(data), width=width or "", height=height or "", sha256=sha)
        if sha in self.by_sha:
            self._row(status="duplicate", file=self.by_sha[sha], **base)
            return "duplicate"
        if len(data) < self.min_bytes:
            self._row(status="too_small", **base)
            return "too_small"
        if self.min_width and width is not None and width < self.min_width:
            self._row(status="too_narrow", **base)
            return "too_narrow"
        name = safe_name(final or url, sha, EXT_BY_TYPE.get(kind))
        host = re.sub(r"[^A-Za-z0-9.-]+", "_", urllib.parse.urlsplit(page_url).netloc) or "files"
        rel = os.path.join(host, name)
        os.makedirs(os.path.join(self.out_dir, host), exist_ok=True)
        with open(os.path.join(self.out_dir, rel), "wb") as f:
            f.write(data)
        self.by_sha[sha] = rel
        self.saved += 1
        self._row(status="saved", file=rel, **base)
        self.log("saved %s (%d bytes)" % (rel, len(data)))
        return "saved"


def _charset(body):
    m = re.search(rb'<meta[^>]+charset=["\']?([A-Za-z0-9_-]+)', body[:4096], re.I)
    if m:
        name = m.group(1).decode("ascii")
        try:
            "".encode(name)
            return name
        except LookupError:
            pass
    return "utf-8"


def _err(e):
    if isinstance(e, urllib.error.HTTPError):
        return "HTTP %d" % e.code
    if isinstance(e, urllib.error.URLError):
        return "URL error: %s" % e.reason
    return "%s: %s" % (type(e).__name__, e)


def summarize(rows, pages=()):
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    saved = [r for r in rows if r["status"] == "saved"]
    return {
        "rows": len(rows),
        "by_status": dict(sorted(counts.items())),
        "saved_bytes": sum(int(r["bytes"]) for r in saved),
        "pages": len(pages),
        "candidates_on_pages": sum(p["candidates"] for p in pages),
        "candidates_not_tried": sum(p["not_tried"] for p in pages),
    }


def write_manifest(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------
# CLI


def read_url_file(path):
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="page_image_downloader.py",
        description="Download every image on a web page (img, srcset, og:image), or a list of image URLs.")
    p.add_argument("urls", nargs="*", help="page URLs and/or direct image URLs")
    p.add_argument("-i", "--input", help="text file with one URL per line (# comments allowed)")
    p.add_argument("-o", "--out", default="images", help="output folder (default: images)")
    p.add_argument("--links", default="", help="also download linked files with these extensions, e.g. pdf,zip")
    p.add_argument("--max-per-page", type=int, default=0, help="save at most N files per page (0 = no limit)")
    p.add_argument("--max-files", type=int, default=0, help="stop after N saved files (0 = no limit)")
    p.add_argument("--min-kb", type=float, default=0, help="skip files smaller than this (tracking pixels, icons)")
    p.add_argument("--min-width", type=int, default=0, help="skip images narrower than this many pixels")
    p.add_argument("--max-mb", type=float, default=10, help="skip files larger than this (default: 10)")
    p.add_argument("--delay", type=float, default=0.5, help="seconds between requests to one host (default: 0.5)")
    p.add_argument("--timeout", type=float, default=30, help="seconds per request (default: 30)")
    p.add_argument("--ignore-robots", action="store_true", help="do not check robots.txt (only for your own sites)")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress lines on stderr")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = p.parse_args(argv)

    urls = list(args.urls)
    if args.input:
        try:
            urls += read_url_file(args.input)
        except OSError as e:
            p.error("cannot read input file %r: %s" % (args.input, e.strerror or e))
    if not urls:
        p.error("give at least one URL, or --input FILE")
    def _scheme(u):
        try:
            return urllib.parse.urlsplit(u).scheme
        except ValueError:
            return ""
    bad = [u for u in urls if _scheme(u) not in ("http", "https")]
    if bad:
        p.error("not an http(s) URL: %s" % bad[0])

    log = (lambda m: None) if args.quiet else (lambda m: print(m, file=sys.stderr, flush=True))
    fetcher = Fetcher(delay=args.delay, timeout=args.timeout)
    robots = None if args.ignore_robots else Robots(fetcher)
    os.makedirs(args.out, exist_ok=True)
    d = Downloader(args.out, fetcher, robots,
                   min_bytes=int(args.min_kb * 1024), min_width=args.min_width,
                   max_bytes=int(args.max_mb * 1024 * 1024), max_per_page=args.max_per_page,
                   max_files=args.max_files,
                   link_extensions=[e.strip() for e in args.links.split(",") if e.strip()], log=log)
    rows = d.run(urls)
    manifest = os.path.join(args.out, "manifest.csv")
    write_manifest(rows, manifest)
    summary = summarize(rows, d.pages)
    summary["manifest"] = manifest
    print(json.dumps(summary, indent=2))
    # Exit 1 only when nothing could be saved and something failed.
    failed = summary["by_status"].get("error", 0)
    return 1 if failed and not summary["by_status"].get("saved") else 0


if __name__ == "__main__":
    sys.exit(main())
