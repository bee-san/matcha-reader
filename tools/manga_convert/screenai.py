#!/usr/bin/env python3
"""Chrome's on-device OCR (Screen AI) for the manga converter.

Screen AI is the OCR Chrome ships for accessibility (reading text in images and scanned PDFs). It
runs locally, reads vertical Japanese, and reports every printed line with its box and every
character with its own. That is what panels.dat stores, so it can stand in for Gemini or for the
manga-ocr text a mokuro volume carries.

The library is Chromium's own `chromium/third_party/screen-ai` package. `download()` fetches it
from Chromium's package server (chrome-infra-packages.appspot.com), checks the archive against the
SHA-256 the server publishes for it, and unpacks it into ~/.config/screen_ai/resources -- the same
place owocr uses, so either one's download serves both.

    python3 screenai.py --setup             # download the library once
    python3 screenai.py page.jpg            # print what it reads, as JSON
    python3 screenai.py --serve             # let the browser converter use it (see serve())
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import platform
import re
import struct
import sys
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DIR = Path.home() / ".config" / "screen_ai" / "resources"
LIBRARY_NAME = "chrome_screen_ai.dll" if sys.platform == "win32" else "libchromescreenai.so"
CIPD_API = "https://chrome-infra-packages.appspot.com/prpc/cipd.Repository/"


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float
    angle: float = 0.0  # degrees, around (x, y)

    def corners(self) -> tuple[float, float, float, float]:
        """Axis-aligned (x1, y1, x2, y2) around the possibly rotated box."""
        import math
        a = math.radians(self.angle)
        ca, sa = math.cos(a), math.sin(a)
        pts = [(self.x + dx * ca - dy * sa, self.y + dx * sa + dy * ca)
               for dx, dy in ((0, 0), (self.w, 0), (self.w, self.h), (0, self.h))]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass
class Line:
    text: str
    box: Box
    block_id: int = 0
    paragraph_id: int = 0
    vertical: bool = False
    confidence: float = 0.0
    symbols: list[tuple[str, Box]] = field(default_factory=list)


# ── Protobuf (only the fields of chrome_screen_ai.proto that are used) ──────────────────────────


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = value = 0
    while True:
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7


def _fields(buf: bytes):
    """(field number, wire type, value) for each field of one message."""
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        elif wire == 2:
            length, i = _varint(buf, i)
            value, i = buf[i:i + length], i + length
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield number, wire, value


def _int32(v: int) -> int:
    return v - (1 << 64) if v >= 1 << 63 else v


def _rect(buf: bytes) -> Box:
    # Rect: x = 1, y = 2, width = 3, height = 4, angle = 5 (float)
    vals = {1: 0, 2: 0, 3: 0, 4: 0}
    angle = 0.0
    for number, wire, value in _fields(buf):
        if number in vals and wire == 0:
            vals[number] = _int32(value)
        elif number == 5 and wire == 5:
            angle = struct.unpack("<f", value)[0]
    return Box(vals[1], vals[2], vals[3], vals[4], angle)


DIRECTION_TOP_TO_BOTTOM = 3


def parse_visual_annotation(buf: bytes) -> list[Line]:
    """VisualAnnotation.lines (field 2) as Lines, with their symbols (word field 1 → symbol)."""
    lines = []
    for number, wire, value in _fields(buf):
        if number != 2 or wire != 2:
            continue
        line = Line("", Box(0, 0, 0, 0))
        for ln, lw, lv in _fields(value):
            if ln == 1 and lw == 2:  # WordBox
                for wn, ww, wv in _fields(lv):
                    if wn == 1 and ww == 2:  # SymbolBox: bounding_box = 1, utf8_string = 2
                        text, box = "", None
                        for sn, sw, sv in _fields(wv):
                            if sn == 1 and sw == 2:
                                box = _rect(sv)
                            elif sn == 2 and sw == 2:
                                text = sv.decode("utf-8", "replace")
                        if box is not None:
                            line.symbols.append((text, box))
            elif ln == 2 and lw == 2:
                line.box = _rect(lv)
            elif ln == 3 and lw == 2:
                line.text = lv.decode("utf-8", "replace")
            elif ln == 5 and lw == 0:
                line.block_id = _int32(lv)
            elif ln == 7 and lw == 0:
                line.vertical = lv == DIRECTION_TOP_TO_BOTTOM
            elif ln == 10 and lw == 5:
                line.confidence = struct.unpack("<f", lv)[0]
            elif ln == 11 and lw == 0:
                line.paragraph_id = _int32(lv)
        lines.append(line)
    return lines


# ── The library ─────────────────────────────────────────────────────────────────────────────────


class _SkColorInfo(ctypes.Structure):
    _fields_ = [("fColorSpace", ctypes.c_void_p), ("fColorType", ctypes.c_int32), ("fAlphaType", ctypes.c_int32)]


class _SkISize(ctypes.Structure):
    _fields_ = [("fWidth", ctypes.c_int32), ("fHeight", ctypes.c_int32)]


class _SkImageInfo(ctypes.Structure):
    _fields_ = [("fColorInfo", _SkColorInfo), ("fDimensions", _SkISize)]


class _SkPixmap(ctypes.Structure):
    _fields_ = [("fPixels", ctypes.c_void_p), ("fRowBytes", ctypes.c_size_t), ("fInfo", _SkImageInfo)]


class _SkBitmap(ctypes.Structure):
    _fields_ = [("fPixelRef", ctypes.c_void_p), ("fPixmap", _SkPixmap), ("fFlags", ctypes.c_uint32)]


SK_RGBA_8888 = 4
SK_ALPHA_OPAQUE = 1


class ScreenAI:
    """One loaded instance of the library. Not thread-safe; create one per process."""

    def __init__(self, resources: Path = DEFAULT_DIR):
        self.resources = Path(resources)
        library = self.resources / LIBRARY_NAME
        if not library.is_file():
            raise FileNotFoundError(f"{library} not found; run `python3 {Path(__file__).name} --setup` first")

        # The library asks for its model files by name through these two callbacks.
        @ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_char_p)
        def file_size(name):
            path = self.resources / name.decode("utf-8")
            return path.stat().st_size if path.is_file() else 0

        @ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_void_p)
        def file_content(name, size, dest):
            path = self.resources / name.decode("utf-8")
            if path.is_file():
                with open(path, "rb") as f:
                    ctypes.memmove(dest, f.read(size), size)

        self._callbacks = (file_size, file_content)  # kept alive for the library's lifetime
        lib = ctypes.CDLL(str(library), mode=getattr(os, "RTLD_LAZY", ctypes.DEFAULT_MODE))
        lib.SetFileContentFunctions.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.InitOCRUsingCallback.restype = ctypes.c_bool
        lib.SetOCRLightMode.argtypes = [ctypes.c_bool]
        lib.PerformOCR.argtypes = [ctypes.POINTER(_SkBitmap), ctypes.POINTER(ctypes.c_uint32)]
        lib.PerformOCR.restype = ctypes.c_void_p
        lib.FreeLibraryAllocatedCharArray.argtypes = [ctypes.c_void_p]
        lib.GetMaxImageDimension.restype = ctypes.c_uint32
        lib.SetFileContentFunctions(file_size, file_content)
        if not lib.InitOCRUsingCallback():
            raise RuntimeError("Screen AI failed to initialise; the downloaded files may be incomplete")
        lib.SetOCRLightMode(False)
        self.lib = lib
        self.max_dimension = int(lib.GetMaxImageDimension()) or 2048

    def read(self, image) -> list[Line]:
        """Lines found in a PIL image, in its own pixel coordinates."""
        from PIL import Image

        scale = 1.0
        if max(image.size) > self.max_dimension:
            scale = self.max_dimension / max(image.size)
            image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                                 Image.LANCZOS)
        rgba = image.convert("RGBA")
        pixels = rgba.tobytes()
        bitmap = _SkBitmap()
        bitmap.fPixmap.fPixels = ctypes.cast(ctypes.c_char_p(pixels), ctypes.c_void_p)
        bitmap.fPixmap.fRowBytes = rgba.width * 4
        bitmap.fPixmap.fInfo.fColorInfo.fColorType = SK_RGBA_8888
        bitmap.fPixmap.fInfo.fColorInfo.fAlphaType = SK_ALPHA_OPAQUE
        bitmap.fPixmap.fInfo.fDimensions.fWidth = rgba.width
        bitmap.fPixmap.fInfo.fDimensions.fHeight = rgba.height
        length = ctypes.c_uint32(0)
        result = self.lib.PerformOCR(ctypes.byref(bitmap), ctypes.byref(length))
        if not result:
            raise RuntimeError("Screen AI could not read the image")
        try:
            data = ctypes.string_at(result, length.value)
        finally:
            self.lib.FreeLibraryAllocatedCharArray(result)
        lines = parse_visual_annotation(data)
        if scale != 1.0:
            for line in lines:
                for box in [line.box] + [b for _, b in line.symbols]:
                    box.x, box.y, box.w, box.h = box.x / scale, box.y / scale, box.w / scale, box.h / scale
        return lines


# ── From lines to speech bubbles ────────────────────────────────────────────────────────────────
#
# Screen AI reports printed lines, and often files the columns of one speech bubble under separate
# blocks. The device wants one text block per bubble -- a saved sentence is the block's text -- so
# lines are regrouped here by how manga lettering is laid out: the columns of a bubble share their
# orientation and size, sit side by side with a gap narrower than a column, and start near the same
# height. Furigana is the same shape at half the size, and is dropped.

_JAPANESE = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f々〆ヵヶ"
_KANA_ONLY = re.compile(f"^[\u3040-\u30ffー・]+$")
_HAS_JAPANESE = re.compile(f"[{_JAPANESE}]")
_DOT_RUNS = re.compile(r"(?:･･･|．．．|・・・|\.\.\.)")
_FULLWIDTH_ALNUM = {cp: cp - 0xFEE0 for lo, hi in ((0xFF10, 0xFF19), (0xFF21, 0xFF3A), (0xFF41, 0xFF5A))
                    for cp in range(lo, hi + 1)}


def line_text(text: str) -> str:
    """One printed cell per printed character: dot runs become the ellipsis they were drawn as."""
    return _DOT_RUNS.sub("…", text.strip()).translate(_FULLWIDTH_ALNUM)


def _thickness(line: Line) -> float:
    x1, y1, x2, y2 = line.box.corners()
    return (x2 - x1) if line.vertical else (y2 - y1)


def _overlap(a1: float, a2: float, b1: float, b2: float) -> float:
    return max(0.0, min(a2, b2) - max(a1, b1))


def _is_noise(text: str) -> bool:
    # Page numbers, stray letters and punctuation-only fragments; real dialogue has Japanese in it.
    return not _HAS_JAPANESE.search(text) and len(text) < 4


def _is_furigana(line: Line, others: list[Line]) -> bool:
    """Kana at about half the size of a line it runs alongside: a reading aid, not dialogue."""
    if not _KANA_ONLY.match(line.text.replace(" ", "")):
        return False
    t = _thickness(line)
    lx1, ly1, lx2, ly2 = line.box.corners()
    for other in others:
        if other is line or other.vertical != line.vertical or _thickness(other) < 1.6 * t:
            continue
        ox1, oy1, ox2, oy2 = other.box.corners()
        if line.vertical:
            beside = _overlap(ly1, ly2, oy1, oy2) > 0.5 * (ly2 - ly1) and (
                abs(lx1 - ox2) < 0.6 * t or abs(ox1 - lx2) < 0.6 * t or _overlap(lx1, lx2, ox1, ox2) > 0)
        else:
            beside = _overlap(lx1, lx2, ox1, ox2) > 0.5 * (lx2 - lx1) and (
                abs(ly2 - oy1) < 0.6 * t or abs(oy2 - ly1) < 0.6 * t or _overlap(ly1, ly2, oy1, oy2) > 0)
        if beside:
            return True
    return False


def _same_bubble(a: Line, b: Line) -> bool:
    if a.vertical != b.vertical:
        return False
    ta, tb = _thickness(a), _thickness(b)
    if not 0.7 <= ta / tb <= 1.43:
        return False
    t = max(ta, tb)
    ax1, ay1, ax2, ay2 = a.box.corners()
    bx1, by1, bx2, by2 = b.box.corners()
    if a.vertical:
        gap = max(bx1 - ax2, ax1 - bx2)
        return gap < 0.75 * t and abs(ay1 - by1) < 1.5 * t and _overlap(ay1, ay2, by1, by2) > 0.3 * min(ay2 - ay1, by2 - by1)
    gap = max(by1 - ay2, ay1 - by2)
    return gap < 0.75 * t and abs(ax1 - bx1) < 1.5 * t and _overlap(ax1, ax2, bx1, bx2) > 0.3 * min(ax2 - ax1, bx2 - bx1)


def bubbles(lines: list[Line]) -> list[dict]:
    """Speech-bubble blocks in the image's pixels: {"box", "text", "lines", "vertical"}, the same shape
    mokuro_text_blocks() returns, lines in reading order (right to left for vertical text)."""
    kept = []
    for line in lines:
        text = line_text(line.text)
        if text and not _is_noise(text):
            kept.append(Line(text, line.box, line.block_id, line.paragraph_id, line.vertical, line.confidence,
                             line.symbols))
    kept = [l for l in kept if not _is_furigana(l, kept)]

    parent = list(range(len(kept)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(kept)):
        for j in range(i + 1, len(kept)):
            same_block = kept[i].block_id == kept[j].block_id and kept[i].vertical == kept[j].vertical
            if same_block or _same_bubble(kept[i], kept[j]):
                parent[find(i)] = find(j)

    groups: dict[int, list[Line]] = {}
    for i, line in enumerate(kept):
        groups.setdefault(find(i), []).append(line)
    out = []
    for group in groups.values():
        vertical = group[0].vertical
        # Columns right to left; rows top to bottom.
        group.sort(key=(lambda l: -l.box.corners()[2]) if vertical else (lambda l: l.box.corners()[1]))
        boxes = [l.box.corners() for l in group]
        out.append({
            "box": [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)],
            "text": "\n".join(l.text for l in group),
            "lines": [list(b) for b in boxes][:255],
            "vertical": vertical,
        })
    return out


# ── Download ────────────────────────────────────────────────────────────────────────────────────


def _cipd(method: str, body: dict) -> dict:
    req = urllib.request.Request(CIPD_API + method, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode()
    return json.loads(text[text.index("{"):])  # pRPC prefixes JSON with ")]}'"


def cipd_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "amd64" if machine in ("x86_64", "amd64") else "arm64" if machine in ("aarch64", "arm64") else "386"
    if system == "linux":
        return "linux"  # the package is published for linux without an architecture suffix
    return f"{'mac' if system == 'darwin' else system}-{arch}"


def download(resources: Path = DEFAULT_DIR) -> Path:
    """Fetch Chromium's screen-ai package and unpack its resources/ folder into `resources`."""
    package = f"chromium/third_party/screen-ai/{cipd_platform()}"
    instance = _cipd("ResolveVersion", {"package": package, "version": "latest"})["instance"]
    url = _cipd("GetInstanceURL", {"package": package, "instance": instance})["signedUrl"]
    print(f"Downloading {package} ({instance['hexDigest'][:12]}…)")
    with urllib.request.urlopen(url, timeout=600) as resp:
        data = resp.read()
    digest = hashlib.sha256(data).hexdigest()
    if instance.get("hashAlgo") != "SHA256" or digest != instance["hexDigest"]:
        raise RuntimeError(f"downloaded package does not match its published SHA-256 ({digest})")
    resources = Path(resources)
    resources.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or not name.startswith("resources/") or ".." in Path(name).parts:
                continue
            target = resources / name[len("resources/"):]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
    (resources / LIBRARY_NAME).chmod(0o755)
    print(f"Screen AI is in {resources}")
    return resources


# ── Local helper for the browser converter ──────────────────────────────────────────────────────

SERVE_PORT = 7393
# Pages allowed to use the helper. Any other site you visit gets no CORS headers, so its scripts
# cannot read what the helper returns.
ALLOWED_ORIGINS = ("https://bee-san.github.io",)
LOCAL_ORIGIN = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$")
MAX_IMAGE_BYTES = 64 << 20


def mokuro_page(lines: list[Line], width: int, height: int) -> dict:
    """One page in mokuro's .mokuro page format, so the browser's mokuro code reads it unchanged."""
    blocks = []
    for b in bubbles(lines):
        quads = [[[l[0], l[1]], [l[2], l[1]], [l[2], l[3]], [l[0], l[3]]] for l in b["lines"]]
        blocks.append({"box": [round(v) for v in b["box"]], "vertical": b["vertical"],
                       "lines": b["text"].split("\n"), "lines_coords": quads})
    return {"img_width": width, "img_height": height, "blocks": blocks}


def serve(engine: "ScreenAI", port: int = SERVE_PORT, extra_origins: tuple[str, ...] = ()) -> None:
    """Answer POST /page (an image file as the body) with that page as mokuro JSON, on 127.0.0.1 only."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from PIL import Image

    lock = threading.Lock()  # one OCR at a time: the library is not thread-safe
    allowed = set(ALLOWED_ORIGINS) | set(extra_origins)

    class Handler(BaseHTTPRequestHandler):
        def _cors(self):
            origin = self.headers.get("Origin", "")
            if origin in allowed or LOCAL_ORIGIN.match(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                # Chrome asks before a public page may reach a local address.
                self.send_header("Access-Control-Allow-Private-Network", "true")

        def _json(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            if self.path == "/status":
                self._json(200, {"engine": "screenai", "ok": True})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/page":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_IMAGE_BYTES:
                self._json(413, {"error": "send one page image, at most 64 MB"})
                return
            try:
                image = Image.open(io.BytesIO(self.rfile.read(length)))
                image.load()
                with lock:
                    lines = engine.read(image)
                self._json(200, mokuro_page(lines, image.width, image.height))
            except Exception as e:  # a bad image must not stop the helper
                self._json(400, {"error": str(e)})

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Screen AI helper on http://127.0.0.1:{port} -- leave this running while the converter reads text. "
          "Ctrl+C stops it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("images", nargs="*", help="Images to read")
    parser.add_argument("--setup", action="store_true", help="Download Chrome's Screen AI library")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="Where the library lives")
    parser.add_argument("--bubbles", action="store_true", help="Print speech-bubble blocks instead of raw lines")
    parser.add_argument("--serve", action="store_true", help="Run the local helper the browser converter uses")
    parser.add_argument("--port", type=int, default=SERVE_PORT, help="Port for --serve")
    parser.add_argument("--allow-origin", action="append", default=[],
                        help="Another page origin allowed to use --serve (e.g. a self-hosted converter)")
    args = parser.parse_args()
    if args.setup or (args.serve and not (args.dir / LIBRARY_NAME).is_file()):
        download(args.dir)
    if args.serve:
        serve(ScreenAI(args.dir), args.port, tuple(args.allow_origin))
        return
    if not args.images:
        return
    from PIL import Image
    engine = ScreenAI(args.dir)
    for path in args.images:
        lines = engine.read(Image.open(path))
        if args.bubbles:
            print(json.dumps({"image": path, "bubbles": [{**b, "box": [round(v) for v in b["box"]],
                                                           "lines": [[round(v) for v in l] for l in b["lines"]]}
                                                          for b in bubbles(lines)]}, ensure_ascii=False, indent=1))
            continue
        print(json.dumps({"image": path, "lines": [
            {"text": l.text, "box": [round(v) for v in l.box.corners()], "block": l.block_id,
             "paragraph": l.paragraph_id, "vertical": l.vertical, "confidence": round(l.confidence, 3),
             "symbols": len(l.symbols)} for l in lines]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
