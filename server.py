#!/usr/bin/env python3
"""Moodboard — local server.

Serves the board UI and does the work a browser page can't:
  * turns any link (Pinterest, Behance, YouTube, Vimeo, Drive, Dropbox,
    a plain page) into the image or video behind it
  * reads public Google Sheets and dropped CSV / XLSX files for links
  * proxies images that block hotlinking
  * saves boards and dropped media to this folder

Run:  python3 server.py            (then open http://localhost:8765)
      python3 server.py 9000       (another port)
Stdlib only, Python 3.8+.
"""
import csv
import hashlib
import html
import io
import json
import mimetypes
import os
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))
BOARDS = os.path.join(ROOT, "boards")
MEDIA = os.path.join(ROOT, "media")
CACHE = os.path.join(ROOT, ".cache")
for d in (BOARDS, MEDIA, CACHE):
    os.makedirs(d, exist_ok=True)

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BOT_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"

IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".bmp", ".jfif")
VID_EXT = (".mp4", ".webm", ".mov", ".m4v", ".ogv")
URL_RE = re.compile(r"https?://[^\s\"'<>,;)\]]+", re.I)


# ---------------------------------------------------------------- helpers
def fetch(url, ua=BROWSER_UA, limit=2_000_000, timeout=10):
    req = Request(url, headers={"User-Agent": ua, "Accept-Language": "en"})
    with urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
        return r.geturl(), ctype, r.read(limit)


def fetch_json(url):
    _, _, body = fetch(url, limit=500_000)
    return json.loads(body.decode("utf-8", "replace"))


def meta_tags(doc):
    """Return {property/name: content} for <meta> tags, first one wins."""
    out = {}
    for tag in re.findall(r"<meta\b[^>]*>", doc, re.I):
        key = re.search(r"(?:property|name|itemprop)\s*=\s*[\"']([^\"']+)", tag, re.I)
        val = re.search(r"content\s*=\s*[\"']([^\"']*)", tag, re.I)
        if key and val:
            out.setdefault(key.group(1).lower(), html.unescape(val.group(1)))
    return out


def page_title(doc, meta):
    t = meta.get("og:title") or meta.get("twitter:title")
    if not t:
        m = re.search(r"<title[^>]*>(.*?)</title>", doc, re.I | re.S)
        t = html.unescape(m.group(1)).strip() if m else ""
    return re.sub(r"\s+", " ", t)[:160]


def ext_of(url):
    return os.path.splitext(urlparse(url).path.lower())[1]


def item(kind, url, src="", **kw):
    d = {"kind": kind, "url": url, "src": src or url}
    d.update({k: v for k, v in kw.items() if v not in (None, "")})
    return d


# ---------------------------------------------------------------- resolver
def resolve(url):
    url = url.strip().rstrip(".")
    p = urlparse(url)
    host = p.netloc.lower().replace("www.", "")
    ext = ext_of(url)

    # YouTube
    m = re.search(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/)|youtu\.be/)([\w-]{11})", url)
    if m:
        vid = m.group(1)
        title, w, h = "", 16, 9
        try:
            o = fetch_json("https://www.youtube.com/oembed?format=json&url=" + quote(url, safe=""))
            title = o.get("title", "")
        except Exception:
            pass
        if "/shorts/" in url:
            w, h = 9, 16
        return item("embed", url, "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid,
                    embed="https://www.youtube-nocookie.com/embed/%s?autoplay=1&rel=0" % vid,
                    title=title, provider="YouTube", w=w, h=h)

    # Vimeo
    m = re.search(r"vimeo\.com/(?:video/|channels/[\w-]+/)?(\d+)", url)
    if m:
        try:
            o = fetch_json("https://vimeo.com/api/oembed.json?width=1280&url=" + quote(url, safe=""))
            return item("embed", url, o.get("thumbnail_url", ""),
                        embed="https://player.vimeo.com/video/%s?autoplay=1" % m.group(1),
                        title=o.get("title", ""), provider="Vimeo",
                        w=o.get("width"), h=o.get("height"))
        except Exception:
            pass

    # Google Drive file
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:.*&)?id=)([\w-]{20,})", url)
    if m:
        fid = m.group(1)
        return item("embed", url, "https://drive.google.com/thumbnail?id=%s&sz=w1600" % fid,
                    embed="https://drive.google.com/file/d/%s/preview" % fid, provider="Drive")

    # Dropbox share link -> raw file
    if "dropbox.com" in host and ext in IMG_EXT + VID_EXT:
        raw = re.sub(r"[?&]dl=\d", "", url)
        raw += ("&" if "?" in raw else "?") + "raw=1"
        return item("video" if ext in VID_EXT else "image", url, raw, provider="Dropbox")

    if ext in IMG_EXT:
        return item("image", url)
    if ext in VID_EXT:
        return item("video", url)

    # Anything else: look at what the server sends back
    last_err = None
    for ua in (BROWSER_UA, BOT_UA):
        try:
            final, ctype, body = fetch(url, ua=ua)
        except Exception as e:
            last_err = e
            continue
        if ctype.startswith("image/"):
            return item("image", url, final)
        if ctype.startswith("video/"):
            return item("video", url, final)
        doc = body.decode("utf-8", "replace")
        meta = meta_tags(doc)
        title = page_title(doc, meta)
        video = (meta.get("og:video:secure_url") or meta.get("og:video:url")
                 or meta.get("og:video") or meta.get("twitter:player:stream"))
        image = (meta.get("og:image:secure_url") or meta.get("og:image")
                 or meta.get("twitter:image") or meta.get("twitter:image:src"))
        if not image:
            lm = re.search(r"<link[^>]+rel=[\"']image_src[\"'][^>]*href=[\"']([^\"']+)", doc, re.I)
            image = html.unescape(lm.group(1)) if lm else ""
        provider = meta.get("og:site_name") or host
        if video and ext_of(video) in VID_EXT:
            return item("video", url, urljoin(final, video), poster=urljoin(final, image) if image else "",
                        title=title, provider=provider)
        if image:
            return item("image", url, urljoin(final, image), title=title, provider=provider,
                        w=_int(meta.get("og:image:width")), h=_int(meta.get("og:image:height")))
        if ua == BROWSER_UA:
            continue  # some sites only show OG tags to link-preview bots
        return item("link", url, "", title=title or host, provider=provider)
    return item("link", url, "", title=host, provider=host,
                error=str(last_err) if last_err else "")


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def safe_resolve(entry):
    url = entry["url"]
    try:
        d = entry.get("item") or resolve(url)
    except Exception as e:
        d = item("link", url, "", title=urlparse(url).netloc, error=str(e))
    if entry.get("caption"):
        d["caption"] = entry["caption"]
    if entry.get("tag"):
        d["tag"] = entry["tag"]
    return d


# ---------------------------------------------------------------- sheets / files
TAG_HEADERS = {"tag", "tags", "group", "section", "category", "theme", "type", "mood"}
CAPTION_HEADERS = {"caption", "note", "notes", "title", "description", "name", "label"}


def rows_to_entries(rows):
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    header = None
    if not any(URL_RE.search(c) for c in rows[0]):
        header = [c.strip().lower() for c in rows[0]]
        rows = rows[1:]
    out = []
    for r in rows:
        urls, texts, tag, caption = [], [], "", ""
        for i, cell in enumerate(r):
            cell = cell.strip()
            found = URL_RE.findall(cell)
            if found:
                urls.extend(found)
                continue
            if not cell:
                continue
            col = header[i] if header and i < len(header) else ""
            if col in TAG_HEADERS and not tag:
                tag = cell
            elif col in CAPTION_HEADERS and not caption:
                caption = cell
            else:
                texts.append(cell)
        caption = caption or " · ".join(texts)
        for u in urls:
            out.append({"url": u, "caption": caption[:200], "tag": tag[:40]})
    return out


def sheet_entries(url):
    m = re.search(r"/spreadsheets/d/(?:e/)?([\w-]+)", url)
    if not m:
        raise ValueError("Not a Google Sheets link")
    gid = re.search(r"[#&?]gid=(\d+)", url)
    if "/d/e/" in url:   # "Publish to web" link
        csv_url = "https://docs.google.com/spreadsheets/d/e/%s/pub?output=csv" % m.group(1)
    else:
        csv_url = "https://docs.google.com/spreadsheets/d/%s/export?format=csv" % m.group(1)
    if gid:
        csv_url += "&gid=" + gid.group(1)
    _, ctype, body = fetch(csv_url, limit=5_000_000)
    if "html" in ctype:
        raise PermissionError("This sheet isn't public. In Google Sheets: Share → General access → "
                              "Anyone with the link → Viewer, then drop the link again.")
    return rows_to_entries(list(csv.reader(io.StringIO(body.decode("utf-8-sig", "replace")))))


def xlsx_entries(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    urls = []
    for name in z.namelist():
        if name.endswith(".xml") or name.endswith(".rels"):
            text = html.unescape(z.read(name).decode("utf-8", "replace"))
            for u in URL_RE.findall(text):
                if "schemas.openxmlformats.org" in u or "schemas.microsoft.com" in u or "purl.org" in u or "w3.org" in u:
                    continue
                if u not in urls:
                    urls.append(u)
    return [{"url": u} for u in urls]


PIN_RESERVED = {"pin", "search", "ideas", "today", "explore", "settings", "business", "_", "categories", "videos", "topics"}


def pinterest_board(u):
    """('user', 'board') for a Pinterest board link, else None."""
    p = urlparse(u)
    if not re.search(r"(^|\.)pinterest\.[a-z.]+$", p.netloc, re.I):
        return None
    seg = [unquote(x) for x in p.path.split("/") if x]
    if len(seg) >= 2 and seg[0].lower() not in PIN_RESERVED:
        return seg[0], seg[1]
    return None


def pinterest_board_entries(user, board):
    """Every pin on a public board, through Pinterest's widget endpoint (up to ~50)."""
    d = fetch_json("https://widgets.pinterest.com/v3/pidgets/boards/%s/%s/pins/" % (quote(user), quote(board)))
    if d.get("status") != "success" or not d.get("data"):
        raise ValueError("That Pinterest board is private or doesn't exist.")
    out = []
    for p in d["data"].get("pins", []):
        imgs = sorted((p.get("images") or {}).values(), key=lambda i: i.get("width", 0), reverse=True)
        if not imgs:
            continue
        im = imgs[0]
        url = "https://www.pinterest.com/pin/%s/" % p["id"]
        out.append({"url": url, "item": item("image", url, im["url"].replace("/564x/", "/736x/"),
                                             title=(p.get("description") or "").strip()[:140],
                                             provider="Pinterest", w=im.get("width"), h=im.get("height"))})
    return out


def expand(urls):
    """Turn a mixed list of links (some of them sheets or Pinterest boards) into entries."""
    entries, notes = [], []
    for u in urls:
        board = None if "/pin/" in u else pinterest_board(u)
        if board:
            try:
                got = pinterest_board_entries(*board)
                entries.extend(got)
                notes.append("Read %d pins from the board." % len(got))
            except Exception as e:
                notes.append(str(e))
        elif "docs.google.com/spreadsheets" in u:
            try:
                got = sheet_entries(u)
                entries.extend(got)
                notes.append("Read %d links from the sheet." % len(got))
            except Exception as e:
                notes.append(str(e))
        else:
            entries.append({"url": u})
    return entries, notes


POOL = ThreadPoolExecutor(max_workers=8)


def resolve_all(entries):
    return list(POOL.map(safe_resolve, entries))


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:60] or "board"


# ---------------------------------------------------------------- http
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("[moodboard] " + (fmt % args) + "\n")

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # ---- GET
    def do_GET(self):
        p = urlparse(self.path)
        path = p.path
        if path == "/":
            self.path = "/index.html"
            return super().do_GET()
        if path == "/api/boards":
            boards = []
            for f in sorted(os.listdir(BOARDS)):
                if f.endswith(".json"):
                    try:
                        with open(os.path.join(BOARDS, f)) as fh:
                            b = json.load(fh)
                        covers = []
                        for it in b.get("items", []):
                            kind = it.get("kind")
                            s = it.get("poster") if kind == "video" else (
                                it.get("src") if kind in ("image", "embed") else None)
                            if s:
                                covers.append(s)
                            if len(covers) == 4:
                                break
                        boards.append({"slug": f[:-5], "name": b.get("name", f[:-5]),
                                       "count": len(b.get("items", [])),
                                       "covers": covers,
                                       "palette": (b.get("palette") or [])[:6],
                                       "updated": b.get("updated", 0)})
                    except Exception:
                        pass
            boards.sort(key=lambda b: -b["updated"])
            return self.send_json(boards)
        if path.startswith("/api/boards/"):
            f = os.path.join(BOARDS, slugify(path.rsplit("/", 1)[1]) + ".json")
            if not os.path.exists(f):
                return self.send_json({"error": "not found"}, 404)
            with open(f) as fh:
                return self.send_json(json.load(fh))
        if path == "/api/img":
            return self.proxy_image(parse_qs(p.query).get("url", [""])[0])
        if path.startswith("/media/"):
            return self.send_file_ranged(os.path.join(MEDIA, os.path.basename(unquote(path))))
        if path in ("/index.html", "/favicon.ico"):
            return super().do_GET()
        self.send_error(404)

    def proxy_image(self, url):
        if not url.startswith(("http://", "https://")):
            return self.send_error(400)
        key = os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest())
        try:
            if os.path.exists(key):
                with open(key + ".type") as fh:
                    ctype = fh.read()
                with open(key, "rb") as fh:
                    data = fh.read()
            else:
                ref = "%s://%s/" % (urlparse(url).scheme, urlparse(url).netloc)
                req = Request(url, headers={"User-Agent": BROWSER_UA, "Referer": ref})
                with urlopen(req, timeout=15) as r:
                    ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
                    data = r.read(25_000_000)
                if not ctype.startswith("image/"):
                    return self.send_error(415)
                with open(key, "wb") as fh:
                    fh.write(data)
                with open(key + ".type", "w") as fh:
                    fh.write(ctype)
        except Exception:
            return self.send_error(502)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=604800")
        self.end_headers()
        self.wfile.write(data)

    def send_file_ranged(self, f):
        if not os.path.isfile(f):
            return self.send_error(404)
        size = os.path.getsize(f)
        ctype = mimetypes.guess_type(f)[0] or "application/octet-stream"
        if f.lower().endswith(".mov"):
            ctype = "video/mp4"  # lets Chromium play H.264 .mov files
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(f, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                left -= len(chunk)

    # ---- POST / PUT / DELETE
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/resolve":
                data = json.loads(self.body() or b"{}")
                urls = []
                for u in data.get("urls", []):
                    for found in URL_RE.findall(u):
                        if found not in urls:
                            urls.append(found)
                entries, notes = expand(urls)
                return self.send_json({"items": resolve_all(entries), "notes": notes})
            if path == "/api/upload":
                name = os.path.basename(unquote(self.headers.get("X-Filename", "file")))
                raw = self.body()
                ext = os.path.splitext(name.lower())[1]
                if ext in (".csv", ".tsv", ".txt"):
                    text = raw.decode("utf-8-sig", "replace")
                    rows = list(csv.reader(io.StringIO(text), delimiter="\t" if ext == ".tsv" else ","))
                    entries = rows_to_entries(rows)
                    return self.send_json({"items": resolve_all(entries),
                                           "notes": ["Read %d links from %s." % (len(entries), name)]})
                if ext == ".xlsx":
                    entries = xlsx_entries(raw)
                    return self.send_json({"items": resolve_all(entries),
                                           "notes": ["Read %d links from %s." % (len(entries), name)]})
                if ext in IMG_EXT + VID_EXT:
                    stem = slugify(os.path.splitext(name)[0])
                    fname = "%s-%s%s" % (stem, hashlib.sha1(raw).hexdigest()[:8], ext)
                    with open(os.path.join(MEDIA, fname), "wb") as fh:
                        fh.write(raw)
                    kind = "video" if ext in VID_EXT else "image"
                    src = "/media/" + fname
                    return self.send_json({"items": [item(kind, src, src, title=name, provider="Local file")],
                                           "notes": []})
                return self.send_json({"items": [], "notes": ["Can't read %s files yet." % ext]})
        except Exception as e:
            return self.send_json({"items": [], "notes": [str(e)]}, 500)
        self.send_error(404)

    def do_PUT(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/boards/"):
            return self.send_error(404)
        board = json.loads(self.body() or b"{}")
        board["updated"] = time.time()
        slug = slugify(path.rsplit("/", 1)[1])
        tmp = os.path.join(BOARDS, slug + ".json.tmp")
        with open(tmp, "w") as fh:
            json.dump(board, fh, indent=1)
        os.replace(tmp, os.path.join(BOARDS, slug + ".json"))
        self.send_json({"ok": True, "slug": slug})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/boards/"):
            f = os.path.join(BOARDS, slugify(path.rsplit("/", 1)[1]) + ".json")
            if os.path.exists(f):
                os.remove(f)
            return self.send_json({"ok": True})
        self.send_error(404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("Moodboard running at http://localhost:%d" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
