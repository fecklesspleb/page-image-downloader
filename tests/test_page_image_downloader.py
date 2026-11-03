import contextlib
import http.client
import csv
import io
import os
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import page_image_downloader as pid  # noqa: E402


# --------------------------------------------------------------------------
# Tiny real image files, built in code so the repo has no binary fixtures.


def png(width, height, pad=0):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(raw))
    if pad:
        out += chunk(b"tEXt", b"pad\x00" + b"x" * pad)
    return out + chunk(b"IEND", b"")


def gif(width, height):
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00" + b"\x3b"


def jpeg(width, height):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def webp_vp8x(width, height):
    payload = b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    body = b"WEBP" + b"VP8X" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp_vp8l(width, height):
    bits = (width - 1) | ((height - 1) << 14)
    payload = b"\x2f" + struct.pack("<I", bits) + b"\x00" * 4
    body = b"WEBP" + b"VP8L" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp_vp8(width, height):
    payload = b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height) + b"\x00" * 4
    body = b"WEBP" + b"VP8 " + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


LOGO = png(120, 40, pad=3000)
SITE = {
    "/robots.txt": (200, "text/plain", b"User-agent: *\nDisallow: /private/\n"),
    "/gallery.html": (200, "text/html; charset=utf-8", open(os.path.join(HERE, "fixtures", "gallery.html"), "rb").read()),
    "/img/share.png": (200, "image/png", png(1200, 630)),
    "/img/logo.png": (200, "image/png", LOGO),
    "/img/logo-copy.png": (200, "image/png", LOGO),
    "/img/hero-1600.png": (200, "image/png", png(1600, 900)),
    "/img/lazy.gif": (200, "image/gif", gif(640, 480) + b"\x00" * 2000),
    "/img/photo@2x.webp": (200, "image/webp", webp_vp8x(2000, 1500)),
    "/img/pixel.gif": (200, "image/gif", gif(1, 1)),
    "/img/broken.jpg": (200, "text/html", b"<html><body>Not found</body></html>"),
    "/private/secret.png": (200, "image/png", png(10, 10)),
    "/img/relative.svg": (200, "image/svg+xml",
                          b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 32"><rect width="64" height="32"/></svg>'),
    "/docs/report.pdf": (200, "application/pdf", b"%PDF-1.4\n%fake test pdf\n"),
    "/docs/members-only.pdf": (200, "text/html", b"<!doctype html><html><body>Please sign in</body></html>"),
    "/direct/photo.jpg": (200, "image/jpeg", jpeg(800, 600)),
    "/big.png": (200, "image/png", png(10, 10, pad=50000)),
    "/bad-urls.html": (200, "text/html", b"""<!doctype html><html><head>
<meta property="og:image" content="http://[oops/share.png">
</head><body>
<img src="http://[oops/x.png" alt="bad IPv6 host">
<img srcset="http://[oops/y.png 2x" alt="bad IPv6 host in srcset">
<a href="http://[oops/r.pdf">bad link</a>
<img src="http://host:abc/x.png" alt="bad port">
<img src="/img/a b.png" alt="space in path">
<img src="/img/truncated.png" alt="cut off">
<img src="/img/logo.png" alt="fine">
</body></html>"""),
}
# Served with a Content-Length larger than the bytes actually sent.
TRUNCATED = {"/img/truncated.png": png(300, 200, pad=4000)}


class Handler(BaseHTTPRequestHandler):
    hits = {}
    flaky = {}  # path -> number of 429 responses still to send

    def do_GET(self):
        Handler.hits[self.path] = Handler.hits.get(self.path, 0) + 1
        if Handler.flaky.get(self.path):
            Handler.flaky[self.path] -= 1
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        if self.path in TRUNCATED:
            body = TRUNCATED[self.path]
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[:len(body) // 2])
            self.close_connection = True
            return
        if self.path not in SITE:
            self.send_response(404)
            self.end_headers()
            return
        code, ctype, body = SITE[self.path]
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class LocalSite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.hits.clear()
        Handler.flaky.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def downloader(self, **kw):
        fetcher = pid.Fetcher(delay=0, timeout=5, sleep=lambda s: None)
        return pid.Downloader(self.out, fetcher, pid.Robots(fetcher), **kw)


# --------------------------------------------------------------------------


class SrcsetTests(unittest.TestCase):
    def test_commas_inside_urls_survive(self):
        v = "https://cdn.example/a.jpg?w=400,h=300 400w, https://cdn.example/a.jpg?w=800,h=600 800w"
        self.assertEqual(pid.parse_srcset(v), [("https://cdn.example/a.jpg?w=400,h=300", "400w"),
                                               ("https://cdn.example/a.jpg?w=800,h=600", "800w")])

    def test_largest_width_wins_regardless_of_order(self):
        self.assertEqual(pid.best_srcset_url("a.jpg 400w, c.jpg 1600w, b.jpg 800w"), "c.jpg")

    def test_largest_density_wins(self):
        self.assertEqual(pid.best_srcset_url("a.jpg, b.jpg 2x, c.jpg 1.5x"), "b.jpg")

    def test_single_url_without_descriptor(self):
        self.assertEqual(pid.best_srcset_url("only.jpg"), "only.jpg")


class FindImagesTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, "fixtures", "gallery.html"), encoding="utf-8") as f:
            self.html = f.read()

    def test_every_kind_of_reference_in_page_order(self):
        found = pid.find_images(self.html, "https://example.com/gallery/index.html")
        urls = [f["url"] for f in found]
        self.assertEqual(urls, [
            "https://example.com/img/share.png",            # og:image (twitter:image is the same URL)
            "https://example.com/img/logo.png",
            "https://example.com/img/hero-1600.png",        # largest srcset candidate
            "https://example.com/img/lazy.gif",             # data-src beats the placeholder src
            "https://example.com/img/photo@2x.webp",        # <picture><source> 2x
            "https://example.com/img/photo.jpg",
            "https://example.com/img/logo-copy.png",
            "https://example.com/img/pixel.gif",
            "https://example.com/img/broken.jpg",
            "https://example.com/private/secret.png",
            "https://example.com/gallery/img/relative.svg",  # relative, fragment dropped
        ])
        by_url = {f["url"]: f for f in found}
        self.assertEqual(by_url["https://example.com/img/logo.png"]["alt"], "Example logo")
        self.assertEqual(by_url["https://example.com/img/hero-1600.png"]["found_in"], "img srcset")
        self.assertEqual(by_url["https://example.com/img/share.png"]["found_in"], "og:image")

    def test_links_only_with_extensions(self):
        self.assertFalse(any(f["found_in"] == "a href" for f in pid.find_images(self.html, "https://example.com/")))
        found = pid.find_images(self.html, "https://example.com/", ["pdf"])
        self.assertEqual([f["url"] for f in found if f["found_in"] == "a href"],
                         ["https://example.com/docs/report.pdf", "https://example.com/docs/members-only.pdf"])

    def test_base_href_is_used(self):
        page = '<base href="https://cdn.example/assets/"><img src="a.png">'
        self.assertEqual(pid.find_images(page, "https://example.com/p")[0]["url"], "https://cdn.example/assets/a.png")

    def test_malformed_urls_are_skipped_not_raised(self):
        html = ('<base href="http://[oops/">'
                '<img src="http://[oops/x.png"><img srcset="http://[oops/y.png 2x">'
                '<meta property="og:image" content="http://[oops/z.png">'
                '<a href="http://[oops/r.pdf">r</a><img src="/ok.png">')
        self.assertEqual([i["url"] for i in pid.find_images(html, "https://x.test/p/", ["pdf"])],
                         ["https://x.test/ok.png"])

    def test_lazy_srcset_beats_placeholder_srcset(self):
        page = '<img srcset="blank.gif 1x" data-srcset="real-300.jpg 300w, real-900.jpg 900w">'
        self.assertEqual(pid.find_images(page, "https://example.com/")[0]["url"], "https://example.com/real-900.jpg")


class SniffAndSizeTests(unittest.TestCase):
    def check(self, data, kind, size):
        self.assertEqual(pid.sniff_type(data), kind)
        self.assertEqual(tuple(pid.image_size(data)), size)

    def test_png(self):
        self.check(png(37, 21), "png", (37, 21))

    def test_gif(self):
        self.check(gif(640, 480), "gif", (640, 480))

    def test_jpeg_skips_app_segments_to_sof(self):
        self.check(jpeg(800, 600), "jpeg", (800, 600))

    def test_webp_all_three_chunk_types(self):
        self.check(webp_vp8x(2000, 1500), "webp", (2000, 1500))
        self.check(webp_vp8l(300, 200), "webp", (300, 200))
        self.check(webp_vp8(320, 240), "webp", (320, 240))

    def test_avif_ispe(self):
        ispe = struct.pack(">I", 20) + b"ispe" + b"\x00\x00\x00\x00" + struct.pack(">II", 1024, 768)
        data = struct.pack(">I", 24) + b"ftypavif" + b"\x00" * 12 + ispe
        self.check(data, "avif", (1024, 768))

    def test_bmp(self):
        data = b"BM" + b"\x00" * 16 + struct.pack("<ii", 50, -40) + b"\x00" * 30
        self.check(data, "bmp", (50, 40))

    def test_svg_width_height_and_viewbox(self):
        self.check(b'<?xml version="1.0"?>\n<svg width="120px" height="60" xmlns="http://www.w3.org/2000/svg"></svg>',
                   "svg", (120, 60))
        self.check(b'<svg viewBox="0 0 64 32"></svg>', "svg", (64, 32))
        self.check(b'<svg width="100%" height="100%"></svg>', "svg", (None, None))
        # MathJax-style: width in ex units, viewBox in user units; the viewBox is not pixels.
        self.check(b'<svg width="13.156ex" height="3.009ex" viewBox="0 -1006.6 5664.6 1295.7"></svg>', "svg", (None, None))

    def test_html_is_not_an_image(self):
        self.assertIsNone(pid.sniff_type(b"<!doctype html><html><body>404</body></html>"))

    def test_truncated_file_does_not_crash(self):
        self.assertEqual(pid.image_size(b"\xff\xd8\xff\xe0\x00"), (None, None))


class SafeNameTests(unittest.TestCase):
    def test_extension_from_content_and_clean_stem(self):
        name = pid.safe_name("https://x.test/media/My%20Photo%20(1).jpeg?x=1", "abcdef0123456789", ".jpg")
        self.assertEqual(name, "My-Photo-1-abcdef0123.jpg")

    def test_no_path(self):
        self.assertEqual(pid.safe_name("https://x.test/", "0" * 64, ".png"), "file-0000000000.png")

    def test_unknown_type_keeps_url_extension(self):
        self.assertEqual(pid.safe_name("https://x.test/r.pdf", "1" * 64, None), "r-1111111111.pdf")


class EndToEndTests(LocalSite):
    def statuses(self, rows):
        return {r["image_url"].replace(self.base, ""): r["status"] for r in rows}

    def test_gallery_page(self):
        d = self.downloader(min_bytes=20)
        rows = d.run([self.base + "/gallery.html"])
        self.assertEqual(self.statuses(rows), {
            "/img/share.png": "saved",
            "/img/logo.png": "saved",
            "/img/hero-1600.png": "saved",
            "/img/lazy.gif": "saved",
            "/img/photo@2x.webp": "saved",
            "/img/photo.jpg": "error",              # 404
            "/img/logo-copy.png": "duplicate",
            "/img/pixel.gif": "too_small",
            "/img/broken.jpg": "not_an_image",
            "/private/secret.png": "robots_disallowed",
            "/img/relative.svg": "saved",
        })
        self.assertNotIn("/private/secret.png", Handler.hits)   # never requested
        self.assertEqual(Handler.hits["/robots.txt"], 1)        # fetched once per origin
        saved = {r["image_url"].replace(self.base, ""): r for r in rows if r["status"] == "saved"}
        self.assertEqual((saved["/img/hero-1600.png"]["width"], saved["/img/hero-1600.png"]["height"]), (1600, 900))
        self.assertEqual((saved["/img/photo@2x.webp"]["width"], saved["/img/relative.svg"]["width"]), (2000, 64))
        self.assertEqual(saved["/img/logo.png"]["alt"], "Example logo")
        for r in saved.values():
            path = os.path.join(self.out, r["file"])
            self.assertTrue(os.path.isfile(path), path)
            self.assertEqual(os.path.getsize(path), r["bytes"])
        dup = next(r for r in rows if r["status"] == "duplicate")
        self.assertEqual(dup["file"], saved["/img/logo.png"]["file"])
        self.assertEqual(d.pages, [{"page_url": self.base + "/gallery.html", "candidates": 11, "not_tried": 0}])

    def test_min_width_and_max_per_page(self):
        d = self.downloader(min_width=1000, max_per_page=2)
        rows = d.run([self.base + "/gallery.html"])
        self.assertEqual([r["image_url"].replace(self.base, "") for r in rows if r["status"] == "saved"],
                         ["/img/share.png", "/img/hero-1600.png"])
        self.assertIn("too_narrow", {r["status"] for r in rows})
        # share.png and hero-1600.png are candidates 1 and 3 of 11; the other 8 are not tried.
        self.assertEqual(d.pages[0]["not_tried"], 8)
        self.assertEqual(pid.summarize(rows, d.pages)["candidates_not_tried"], 8)

    def test_max_files_across_inputs(self):
        d = self.downloader(max_files=1)
        rows = d.run([self.base + "/gallery.html", self.base + "/direct/photo.jpg"])
        self.assertEqual(sum(r["status"] == "saved" for r in rows), 1)
        self.assertNotIn("/direct/photo.jpg", Handler.hits)

    def test_direct_image_url_and_too_large(self):
        d = self.downloader(max_bytes=20000)
        rows = d.run([self.base + "/direct/photo.jpg", self.base + "/big.png"])
        self.assertEqual([(r["found_in"], r["status"], r["width"]) for r in rows],
                         [("direct", "saved", 800), ("direct", "too_large", "")])

    def test_linked_pdf(self):
        d = self.downloader(link_extensions=["pdf"])
        rows = d.run([self.base + "/gallery.html"])
        pdf = next(r for r in rows if r["found_in"] == "a href")
        self.assertEqual((pdf["status"], pdf["content_type"]), ("saved", "application/pdf"))
        self.assertTrue(pdf["file"].endswith(".pdf"))
        wall = next(r for r in rows if r["image_url"].endswith("/members-only.pdf"))
        self.assertEqual((wall["status"], wall["file"]), ("html_instead_of_file", ""))

    def test_retry_on_429_then_success(self):
        Handler.flaky["/direct/photo.jpg"] = 2
        rows = self.downloader().run([self.base + "/direct/photo.jpg"])
        self.assertEqual(rows[0]["status"], "saved")
        self.assertEqual(Handler.hits["/direct/photo.jpg"], 3)

    def test_404_is_not_retried(self):
        rows = self.downloader().run([self.base + "/nope.png"])
        self.assertEqual((rows[0]["status"], rows[0]["error"]), ("error", "HTTP 404"))
        self.assertEqual(Handler.hits["/nope.png"], 1)

    def test_cli_writes_manifest_and_summary(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = pid.main([self.base + "/gallery.html", "-o", self.out, "--delay", "0", "-q", "--min-kb", "0.02"])
        self.assertEqual(code, 0)
        self.assertIn('"saved": 6', buf.getvalue())
        self.assertIn('"candidates_on_pages": 11', buf.getvalue())
        with open(os.path.join(self.out, "manifest.csv"), encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 11)
        self.assertEqual(list(rows[0].keys()), pid.MANIFEST_FIELDS)

    def test_malformed_urls_do_not_stop_the_run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = pid.main([self.base + "/bad-urls.html", "-o", self.out, "--delay", "0", "-q", "--links", "pdf"])
        self.assertEqual(code, 0)
        with open(os.path.join(self.out, "manifest.csv"), encoding="utf-8") as f:
            rows = {r["image_url"].replace(self.base, ""): r for r in csv.DictReader(f)}
        # "http://[oops..." can't be parsed at all, so it is dropped; the rest get a row each.
        self.assertEqual({k: v["status"] for k, v in rows.items()}, {
            "http://host:abc/x.png": "error",
            "/img/a b.png": "error",
            "/img/truncated.png": "error",
            "/img/logo.png": "saved",
        })
        self.assertIn("invalid URL", rows["http://host:abc/x.png"]["error"])
        self.assertIn("InvalidURL", rows["/img/a b.png"]["error"])
        self.assertIn("IncompleteRead", rows["/img/truncated.png"]["error"])
        self.assertIn('"candidates_on_pages": 4', buf.getvalue())

    def test_truncated_download_is_not_saved(self):
        rows = self.downloader().run([self.base + "/img/truncated.png"])
        self.assertEqual((rows[0]["status"], rows[0]["file"]), ("error", ""))
        self.assertIn("IncompleteRead", rows[0]["error"])
        self.assertEqual(os.listdir(self.out), [])

    def test_cli_rejects_unparsable_url_argument(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            pid.main(["http://[oops/x.png", "-o", self.out, "-q"])
        self.assertEqual(cm.exception.code, 2)

    def test_cli_exit_1_when_everything_fails(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pid.main([self.base + "/nope.png", "-o", self.out, "--delay", "0", "-q"]), 1)


class RobotsTests(unittest.TestCase):
    class FakeFetcher:
        def __init__(self, code):
            self.code = code

        def get(self, url, max_bytes):
            raise urllib.error.HTTPError(url, self.code, "x", {}, None)

    def test_missing_robots_allows(self):
        self.assertTrue(pid.Robots(self.FakeFetcher(404)).allowed("https://a.test/x.png"))

    def test_server_error_disallows(self):
        self.assertFalse(pid.Robots(self.FakeFetcher(503)).allowed("https://a.test/x.png"))

    def test_truncated_robots_disallows(self):
        class Truncated:
            def get(self, url, max_bytes):
                raise http.client.IncompleteRead(b"User-agent", 50)
        self.assertFalse(pid.Robots(Truncated()).allowed("https://a.test/x.png"))


class FetcherDelayTests(unittest.TestCase):
    def test_same_host_is_spaced(self):
        slept, now = [], [100.0]

        class Opener:
            def open(self, req, timeout):
                raise urllib.error.HTTPError(req.full_url, 404, "x", {}, None)

        f = pid.Fetcher(delay=0.5, opener=Opener(), sleep=slept.append, clock=lambda: now[0])
        for url in ("https://a.test/1", "https://a.test/2", "https://b.test/1"):
            with self.assertRaises(urllib.error.HTTPError):
                f.get(url, 10)
        self.assertEqual(slept, [0.5])


if __name__ == "__main__":
    unittest.main()
