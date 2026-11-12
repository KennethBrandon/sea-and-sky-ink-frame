"""Read a photo of the color test card (`render/card.py`): `scripts/analyze-card.py`.

1. Decode the photo to linear light (`load_photo`), converting Display P3, as iPhones
   shoot, to sRGB without clipping.
2. Find the four corner markers and work out which way up the card is (`locate`).
3. Measure every patch, by its median if solid and its mean if dotted (`measure`).
4. Remove uneven light with a smooth surface fitted to the white patches, then express
   everything relative to the panel: black ink 0, white ink 1 (`solve`). The eye adapts to
   the panel's own white, and this is also the scale `palette_colors` works on.
5. Report the inks, how well dots mix by linear light and by sRGB, and how far each
   dithered target lands from its color, as ΔE (CIE 1976; about 2 is a just visible
   difference side by side).

A phone isn't a colorimeter: a normal photo's tone curve bends these numbers, so they're
estimates, compared across photos and lights. A RAW file (.dng) avoids most of that. Pure
Pillow/NumPy, plus pillow-heif for HEIC and rawpy for RAW on the Mac.
"""

from __future__ import annotations

import io
import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageCms, ImageDraw, ImageOps

from photopainter.render.card import MARKERS, PATCHES, Patch, card_indices, ink_shares
from photopainter.render.layout import PANEL_SIZE, _register_heif
from photopainter.render.palette import INKS, linear_to_srgb, palette_colors, srgb_to_linear

MAX_DETECT_SIDE = 2400  # markers are looked for at this size at most
MAX_PHOTO_SIDE = 6000  # bigger photos are halved first (a 48 MP photo needs nothing more)
RAW_SUFFIXES = (".dng",)
RAW_TOP = 99.5  # a RAW file's exposure means nothing here: this percentile is scaled to 1
PATTERN_TOLERANCE = 0.5  # how far each marker stripe may be from its share of the width
DARK_FACTORS = (0.65, 0.5, 0.8)  # dark = below this share of the local mean brightness
MARKER_SIZE_RATIO = 1.6  # four markers of one card can't differ in size by more than this
# A fit error (`_fit_error`) under this means the card has been found. On a real shot the
# right way up read 0.18 (JPEG) and 0.35 (RAW); the wrong ways, 0.77 and up.
GOOD_FIT = 0.5
GLARE_BLACK = 0.45  # black ink reads brighter than this (sRGB 0..1): probably glare
CLIPPED_WHITE = 0.98  # white ink reads brighter than this: overexposed
UNEVEN_REPLICATES = 4.0  # ΔE between copies of one ink that's worth a warning

# Display P3 to sRGB, both linear and D65.
P3_TO_SRGB = np.array([[1.2249401, -0.2249404, 0.0],
                       [-0.0420569, 1.0420571, 0.0],
                       [-0.0196376, -0.0786361, 1.0982735]])
_SRGB_TO_XYZ = np.array([[0.4124564, 0.3575761, 0.1804375],
                         [0.2126729, 0.7151522, 0.0721750],
                         [0.0193339, 0.1191920, 0.9503041]])
_D65 = _SRGB_TO_XYZ.sum(axis=1)
INK_NAMES = tuple(ink.name for ink in INKS)


class CardNotFound(ValueError):
    """The photo's corner markers couldn't be found. The message says what to try."""


@dataclass
class Photo:
    path: Path
    linear: np.ndarray  # (h, w, 3) float32, linear sRGB; may go outside 0..1 from P3
    gray: Image.Image  # 8-bit, for finding the markers
    color_space: str


@dataclass
class Measured:
    """One photo's raw patch readings, before `solve`."""

    photo: Photo
    homography: np.ndarray  # card coordinates -> photo pixels
    corners: list[tuple[float, float]]  # marker centers in photo pixels, card clockwise order
    rotation: int  # degrees the card is turned clockwise in the photo
    values: dict[Patch, np.ndarray]  # linear rgb
    warnings: list[str] = field(default_factory=list)


@dataclass
class Solved:
    """One photo's readings relative to the panel: black ink 0, white ink 1, in linear light."""

    relative: dict[Patch, np.ndarray]
    inks: np.ndarray  # (6, 3) in INKS order
    evenness: float  # light across the panel: (brightest - darkest) / mean, from the whites
    warnings: list[str] = field(default_factory=list)


# --- reading the photo ------------------------------------------------------------------


def load_photo(path: str | Path) -> Photo:
    """A photo as linear sRGB, upright (EXIF), with HEIC if pillow-heif is installed and
    RAW if rawpy is."""
    _register_heif()
    path = Path(path)
    if path.suffix.lower() in RAW_SUFFIXES:
        return _load_raw(path)
    img = Image.open(path)
    icc = img.info.get("icc_profile")
    img = ImageOps.exif_transpose(img).convert("RGB")
    if max(img.size) > MAX_PHOTO_SIDE:
        img = img.reduce(2)
    space = _profile_name(icc)
    p3 = "P3" in space
    if icc and not p3 and "sRGB" not in space:
        img = ImageCms.profileToProfile(img, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                        ImageCms.createProfile("sRGB"), outputMode="RGB")
    linear = srgb_to_linear(np.asarray(img, dtype=np.float32) / 255).astype(np.float32)
    if p3:  # the sRGB curve, P3 primaries
        linear = linear @ P3_TO_SRGB.astype(np.float32).T
    return Photo(path, linear, _detection_gray(img.convert("L")), space)


def _detection_gray(gray: Image.Image) -> Image.Image:
    factor = int(np.ceil(max(gray.size) / MAX_DETECT_SIDE))
    return gray.reduce(factor) if factor > 1 else gray


def _load_raw(path: Path) -> Photo:
    """A RAW file as linear sRGB: what the sensor saw, in the shot's own white balance,
    before the phone's tone curve and sharpening. Colors beyond sRGB are clipped."""
    try:
        import rawpy
    except ImportError:
        raise OSError(f"{path.name}: reading RAW needs rawpy (.venv/bin/pip install -e '.[raw]')") from None
    with rawpy.imread(str(path)) as raw:
        rgb = raw.postprocess(gamma=(1, 1), no_auto_bright=True, output_bps=16, use_camera_wb=True,
                              output_color=rawpy.ColorSpace.sRGB,
                              half_size=max(raw.sizes.width, raw.sizes.height) > MAX_PHOTO_SIDE)
    linear = rgb.astype(np.float32) / 65535
    linear /= max(float(np.percentile(linear, RAW_TOP)), 1e-6)
    luminance = linear @ _SRGB_TO_XYZ[1].astype(np.float32)
    gray = Image.fromarray((linear_to_srgb(np.clip(luminance, 0, 1)) * 255 + 0.5).astype(np.uint8))
    return Photo(path, linear, _detection_gray(gray), "RAW, linear")


def _profile_name(icc: bytes | None) -> str:
    if not icc:
        return "sRGB (no profile in the file)"
    try:
        return ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip()
    except (OSError, ImageCms.PyCMSError):
        return "sRGB (unreadable profile)"


# --- finding the corner markers ---------------------------------------------------------


def _dark_mask(gray: np.ndarray, factor: float) -> np.ndarray:
    """Pixels darker than `factor` of the mean around them (a window much wider than a marker)."""
    h, w = gray.shape
    r = max(h, w) // 12
    table = np.zeros((h + 1, w + 1), dtype=np.float64)
    table[1:, 1:] = gray.cumsum(0, dtype=np.float64).cumsum(1)
    y0, y1 = np.clip(np.arange(h) - r, 0, h), np.clip(np.arange(h) + r + 1, 0, h)
    x0, x1 = np.clip(np.arange(w) - r, 0, w), np.clip(np.arange(w) + r + 1, 0, w)
    total = table[y1][:, x1] - table[y0][:, x1] - table[y1][:, x0] + table[y0][:, x0]
    area = (y1 - y0)[:, None] * (x1 - x0)[None, :]
    return gray < factor * total / area


def _pattern_hits(dark: np.ndarray) -> list[tuple[float, float]]:
    """(center, module size) of each dark-light-dark-light-dark run of 1:1:3:1:1 along a line."""
    edges = np.flatnonzero(dark[1:] != dark[:-1]) + 1
    if edges.size < 5:
        return []
    edges = np.r_[0, edges, dark.size]
    runs = np.diff(edges)
    w = np.lib.stride_tricks.sliding_window_view(runs, 5).astype(np.float64)
    module = w.sum(axis=1) / 7
    expected = module[:, None] * np.array([1, 1, 3, 1, 1])
    fits = (np.abs(w - expected) < expected * PATTERN_TOLERANCE).all(axis=1) & (module >= 2)
    starts_dark = (np.arange(len(w)) % 2 == 0) == bool(dark[0])
    return [(edges[i + 2] + w[i, 2] / 2, module[i]) for i in np.flatnonzero(fits & starts_dark)]


def _nearest(hits: list[tuple[float, float]], at: float, within: float) -> tuple[float, float] | None:
    close = [h for h in hits if abs(h[0] - at) < within]
    return min(close, key=lambda h: abs(h[0] - at)) if close else None


def find_markers(gray: Image.Image) -> list[tuple[float, float, float]]:
    """Corner marker candidates in `gray`'s pixels, as (x, y, module size): zero or more, not
    sorted. Every darkness level is tried, because glare can hide a marker at one level
    while something that isn't a marker passes at it; `locate` picks the real four."""
    array = np.asarray(gray, dtype=np.float64)
    found: list[tuple[float, float, float]] = []
    for factor in DARK_FACTORS:
        dark = _dark_mask(array, factor)
        clusters: list[dict[str, Any]] = []
        for y in range(dark.shape[0]):
            for x, module in _pattern_hits(dark[y]):
                for c in clusters:
                    if abs(x - c["x"]) < c["m"] and y - c["last"] <= max(2.0, c["m"] / 2):
                        c["x"], c["last"] = (c["x"] * c["n"] + x) / (c["n"] + 1), y
                        c["n"] += 1
                        break
                else:
                    clusters.append({"x": x, "first": y, "last": y, "m": module, "n": 1})
        for c in clusters:  # a real marker also reads 1:1:3:1:1 up and down through its center
            v = _nearest(_pattern_hits(dark[:, round(c["x"])]), (c["first"] + c["last"]) / 2, c["m"] * 1.5)
            if v is None:
                continue
            h = _nearest(_pattern_hits(dark[round(v[0])]), c["x"], c["m"] * 1.5)
            if h is None or not 0.5 < v[1] / h[1] < 2:
                continue
            if all(np.hypot(h[0] - fx, v[0] - fy) > h[1] * 3 for fx, fy, _ in found):
                found.append((h[0], v[0], (h[1] + v[1]) / 2))
    return found


def _homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """The 3x3 matrix taking four `src` points to four `dst` points."""
    rows, values = [], []
    for (x, y), (u, v) in zip(src, dst):
        rows += [[x, y, 1, 0, 0, 0, -u * x, -u * y], [0, 0, 0, x, y, 1, -v * x, -v * y]]
        values += [u, v]
    return np.append(np.linalg.solve(np.array(rows), np.array(values)), 1).reshape(3, 3)


def _project(h: np.ndarray, points: np.ndarray) -> np.ndarray:
    out = np.c_[points, np.ones(len(points))] @ h.T
    return out[:, :2] / out[:, 2:]


def _clockwise(points: list[tuple[float, float]]) -> np.ndarray:
    """Four points in clockwise order as seen on screen (y grows downward)."""
    p = np.array(points, dtype=np.float64)
    angle = np.arctan2(*(p - p.mean(axis=0)).T[::-1])
    return p[np.argsort(angle)]


def _sample_pixels(linear: np.ndarray, h: np.ndarray, rect: tuple[int, int, int, int]) -> np.ndarray:
    """The photo's pixels inside `rect` (card coordinates), as (n, 3)."""
    x0, y0, x1, y1 = rect
    corners = _project(h, np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float64))
    left, top = np.floor(corners.min(axis=0)).astype(int)
    right, bottom = np.ceil(corners.max(axis=0)).astype(int)
    height, width = linear.shape[:2]
    if left < 0 or top < 0 or right > width or bottom > height:
        return np.empty((0, 3), dtype=np.float32)
    yy, xx = np.mgrid[top:bottom, left:right]
    card = _project(np.linalg.inv(h), np.c_[xx.ravel() + 0.5, yy.ravel() + 0.5])
    inside = ((card[:, 0] >= x0) & (card[:, 0] < x1) & (card[:, 1] >= y0) & (card[:, 1] < y1))
    return linear[top:bottom, left:right].reshape(-1, 3)[inside]


def _fit_error(linear: np.ndarray, h: np.ndarray) -> float:
    """How unlike the expected inks the solid patches look under `h` (lower is better)."""
    solids = [p for p in PATCHES if p.ink is not None]
    medians = {}
    for patch in solids:
        pixels = _sample_pixels(linear, h, patch.sample)
        if pixels.size == 0:
            return float("inf")
        medians[patch] = np.median(pixels, axis=0)
    white = np.median([v for p, v in medians.items() if p.ink == "white"], axis=0)
    expected = np.array(palette_colors("estimated"), dtype=np.float64) / 255
    errors = [np.linalg.norm(linear_to_srgb(np.clip(v / np.maximum(white, 1e-6), 0, 1))
                             - expected[INK_NAMES.index(p.ink)]) for p, v in medians.items()]
    return float(np.mean(errors))


def locate(photo: Photo, corners: list[tuple[float, float]] | None = None
           ) -> tuple[np.ndarray, list[tuple[float, float]], int, float]:
    """(card -> photo homography, marker centers in card order, clockwise turn, fit error).

    `corners` are the marker centers in photo pixels, in any order, when finding them by
    eye was needed. Tries each way up, and sets of four candidates of about one size, biggest
    first (dither patterns can pass for tiny markers), and keeps the one whose solid patches
    look most like the inks, stopping at the first set that looks right.
    """
    scale = photo.linear.shape[1] / photo.gray.size[0]
    retake = ("Retake it with the whole card in view, sharp and without glare, or pass the "
              "markers' centers with --corners.")
    if corners is None:
        found = [(x * scale, y * scale, m) for x, y, m in find_markers(photo.gray)]
        if len(found) < 4:
            raise CardNotFound(f"{photo.path.name}: found {len(found)} of the 4 corner markers. {retake}")
    else:
        if len(corners) != 4:
            raise CardNotFound(f"--corners needs 4 points, got {len(corners)}")
        found = [(x, y, 1.0) for x, y in corners]
    card = np.array(MARKERS, dtype=np.float64)
    best: tuple[float, np.ndarray, np.ndarray, int] | None = None
    for group in itertools.combinations(sorted(found, key=lambda c: -c[2]), 4):
        if best is not None and best[0] < GOOD_FIT:
            break
        sizes = [m for _, _, m in group]
        if max(sizes) > MARKER_SIZE_RATIO * min(sizes):
            continue
        ordered = _clockwise([(x, y) for x, y, _ in group])
        for turn in range(4):
            placed = np.roll(ordered, -turn, axis=0)
            try:
                h = _homography(card, placed)
            except np.linalg.LinAlgError:
                continue
            error = _fit_error(photo.linear, h)
            if best is None or error < best[0]:
                best = (error, h, placed, turn)
    if best is None or not np.isfinite(best[0]):
        raise CardNotFound(f"{photo.path.name}: no four of the {len(found)} markers found fit the card. {retake}")
    error, h, placed, turn = best
    return h, [tuple(p) for p in placed], turn * 90, error


def measure(photo: Photo, corners: list[tuple[float, float]] | None = None) -> Measured:
    """Find the card in `photo` and read every patch."""
    h, placed, rotation, error = locate(photo, corners)
    warnings = []
    if error > GOOD_FIT:
        warnings.append(f"the solid patches look unlike the inks (error {error:.2f}), so the card "
                        f"may be misread: check the overlay image")
    values = {}
    for patch in PATCHES:
        pixels = _sample_pixels(photo.linear, h, patch.sample)
        if pixels.size == 0:
            raise CardNotFound(f"{photo.path.name}: part of the card is outside the photo")
        values[patch] = np.median(pixels, axis=0) if patch.ink is not None else pixels.mean(axis=0)
    return Measured(photo, h, placed, rotation, values, warnings)


# --- from readings to the panel's own scale ---------------------------------------------


def lab(linear_rgb) -> np.ndarray:
    """CIELAB (D65) of linear sRGB, where (1, 1, 1) is the panel's white: L* 100."""
    t = np.maximum(np.asarray(linear_rgb, dtype=np.float64), 0) @ _SRGB_TO_XYZ.T / _D65
    f = np.where(t > 216 / 24389, np.cbrt(t), (24389 / 27 * t + 16) / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def delta_e(a, b) -> float:
    return float(np.linalg.norm(lab(a) - lab(b)))


def _surface_terms(points: np.ndarray) -> np.ndarray:
    x = points[:, 0] / PANEL_SIZE[0] * 2 - 1
    y = points[:, 1] / PANEL_SIZE[1] * 2 - 1
    return np.c_[np.ones(len(x)), x, y, x * x, x * y, y * y]


def solve(values: dict[Patch, np.ndarray]) -> Solved:
    """Readings relative to the panel. Light is taken as evenly multiplying everything, so
    each patch is divided by a smooth surface through the whites at that spot."""
    whites = [p for p in values if p.ink == "white"]
    terms = _surface_terms(np.array([p.center for p in whites], dtype=np.float64))
    readings = np.array([values[p] for p in whites], dtype=np.float64)
    coefficients, *_ = np.linalg.lstsq(terms, readings, rcond=None)
    at_whites = terms @ coefficients
    patches = list(values)
    light = _surface_terms(np.array([p.center for p in patches], dtype=np.float64)) @ coefficients
    level = {p: np.asarray(values[p], dtype=np.float64) / np.maximum(light[i], 1e-6)
             for i, p in enumerate(patches)}

    black = np.median([v for p, v in level.items() if p.ink == "black"], axis=0)
    relative = {p: (v - black) / (1 - black) for p, v in level.items()}
    inks = np.zeros((len(INKS), 3))
    warnings = []
    for i, name in enumerate(INK_NAMES):
        copies = [v for p, v in relative.items() if p.ink == name]
        inks[i] = np.median(copies, axis=0)
        spread = max((delta_e(a, b) for a, b in itertools.combinations(copies, 2)), default=0.0)
        if spread > UNEVEN_REPLICATES:
            warnings.append(f"the {name} patches differ across the panel by ΔE {spread:.1f}: "
                            f"glare, or a shadow?")
    inks[INK_NAMES.index("black")] = 0
    inks[INK_NAMES.index("white")] = 1

    green = at_whites[:, 1]
    evenness = float((green.max() - green.min()) / green.mean())
    white_srgb = linear_to_srgb(np.clip(np.median(readings, axis=0), 0, 1))
    black_srgb = linear_to_srgb(np.clip(np.median([values[p] for p in values if p.ink == "black"], axis=0), 0, 1))
    if white_srgb.max() > CLIPPED_WHITE:
        warnings.append("white ink is at the top of the camera's range: the photo is overexposed")
    if black_srgb.min() > GLARE_BLACK:
        warnings.append("black ink reads as gray: probably glare on the panel")
    return Solved(relative, inks, evenness, warnings)


def srgb_levels(linear) -> np.ndarray:
    """Linear light on the panel's scale as 0..255 sRGB levels, clipped."""
    return np.round(linear_to_srgb(np.clip(linear, 0, 1)) * 255).astype(int)


# --- the report --------------------------------------------------------------------------


@dataclass
class Report:
    photos: list[dict[str, Any]]
    inks: dict[str, list[int]]  # the palette to use, in 0..255 sRGB, black 0 and white 255
    current: dict[str, list[int]]  # what the model has now
    mixes: list[dict[str, Any]]
    linear_mix_error: float  # mean ΔE of the patches from the linear-light mix of their inks
    srgb_mix_error: float  # …from the sRGB mix, which today's dither assumes
    targets: list[dict[str, Any]]
    target_error: dict[str, dict[str, float]]  # method -> all/grays/colors -> mean ΔE
    model_error: float  # mean ΔE of dithered targets from the linear mix of their real dots

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def _average(results: list[Solved], patch: Patch) -> np.ndarray:
    return np.mean([r.relative[patch] for r in results], axis=0)


def build_report(measured: list[Measured], indices: np.ndarray | None = None) -> Report:
    """Everything worth knowing from one or more photos of the card (averaged)."""
    indices = card_indices() if indices is None else indices
    solved = [solve(m.values) for m in measured]
    inks = np.mean([s.inks for s in solved], axis=0)

    photos = []
    for m, s in zip(measured, solved):
        photos.append({
            "file": m.photo.path.name,
            "color_space": m.photo.color_space,
            "rotation": m.rotation,
            "evenness": round(s.evenness, 3),
            "inks": {n: srgb_levels(s.inks[i]).tolist() for i, n in enumerate(INK_NAMES)},
            "warnings": m.warnings + s.warnings,
        })

    srgb_inks = linear_to_srgb(np.clip(inks, 0, 1))
    mixes = []
    for patch in (p for p in PATCHES if p.mix is not None):
        got = _average(solved, patch)
        shares = ink_shares(indices, patch)
        by_linear = shares @ inks
        by_srgb = srgb_to_linear(shares @ srgb_inks)
        mixes.append({"name": patch.mix.name, "measured": srgb_levels(got).tolist(),
                      "linear": srgb_levels(by_linear).tolist(), "srgb": srgb_levels(by_srgb).tolist(),
                      "linear_de": round(delta_e(got, by_linear), 1), "srgb_de": round(delta_e(got, by_srgb), 1)})

    errors: dict[str, dict[str, list[float]]] = {
        m: {"all": [], "grays": [], "colors": []} for m in ("today", "linear")}
    targets: dict[str, dict[str, Any]] = {}
    model = []
    for patch in (p for p in PATCHES if p.target is not None):
        got = _average(solved, patch)
        want = srgb_to_linear(np.array(patch.target.rgb) / 255)
        de = delta_e(got, want)
        group = "grays" if patch.target.name.startswith("gray") else "colors"
        errors[patch.method]["all"].append(de)
        errors[patch.method][group].append(de)
        model.append(delta_e(got, ink_shares(indices, patch) @ inks))
        row = targets.setdefault(patch.target.name, {"name": patch.target.name, "target": list(patch.target.rgb)})
        row[patch.method] = {"measured": srgb_levels(got).tolist(), "de": round(de, 1)}

    return Report(
        photos=photos,
        inks={n: srgb_levels(inks[i]).tolist() for i, n in enumerate(INK_NAMES)},
        current={n: list(c) for n, c in zip(INK_NAMES, palette_colors("estimated"))},
        mixes=mixes,
        linear_mix_error=round(float(np.mean([m["linear_de"] for m in mixes])), 1),
        srgb_mix_error=round(float(np.mean([m["srgb_de"] for m in mixes])), 1),
        targets=list(targets.values()),
        target_error={m: {g: round(float(np.mean(v)), 1) for g, v in groups.items()}
                      for m, groups in errors.items()},
        model_error=round(float(np.mean(model)), 1),
    )


def _rgb(values: list[int]) -> str:
    return "(%3d,%3d,%3d)" % tuple(values)


def report_text(report: Report) -> str:
    """The report for reading in a terminal."""
    out: list[str] = []
    for p in report.photos:
        out.append(f"{p['file']}: {p['color_space']}, card turned {p['rotation']}° clockwise, "
                   f"light varies {p['evenness'] * 100:.0f}% across it")
        out += [f"  ! {w}" for w in p["warnings"]]
    if len(report.photos) > 1:
        out += ["", "Inks in each photo"]
        for name in INK_NAMES:
            out.append(f"  {name:7}" + "  ".join(_rgb(p["inks"][name]) for p in report.photos))

    out += ["", "Inks (black 0, white 255)", f"  {'':7}{'measured':15}{'model now':15}"]
    for name in INK_NAMES:
        out.append(f"  {name:7}{_rgb(report.inks[name]):15}{_rgb(report.current[name]):15}")

    out += ["", "Do dots mix by linear light? (mean ΔE of the patch from each prediction)",
            f"  linear light {report.linear_mix_error:.1f}, sRGB {report.srgb_mix_error:.1f}: "
            + ("closer to linear light, so today's dither should run too light"
               if report.linear_mix_error < report.srgb_mix_error
               else "closer to sRGB, so today's dither is about right"),
            f"  {'':24}{'measured':15}{'linear':15}{'sRGB':15}"]
    for m in report.mixes:
        out.append(f"  {m['name']:24}{_rgb(m['measured']):15}{_rgb(m['linear']):15}{_rgb(m['srgb']):15}")

    out += ["", "Do dots bleed? The 1, 2 and 4 px 50% patches should match if they don't:"]
    fifty = [m for m in report.mixes if m["name"].startswith("black/white 50%")]
    out.append("  " + ", ".join(f"{_rgb(m['measured'])}" for m in fifty))

    errors = report.target_error
    out += ["", "How close dithered colors get (mean ΔE, lower is better)",
            f"  {'':16}{'today':>8}{'linear':>8}"]
    for group in ("all", "grays", "colors"):
        out.append(f"  {group:16}{errors['today'][group]:8.1f}{errors['linear'][group]:8.1f}")
    out += [f"  linear-mix model vs the panel, with the measured inks: ΔE {report.model_error:.1f}", "",
            f"  {'':12}{'target':15}{'today':15}{'ΔE':>5}  {'linear':15}{'ΔE':>5}"]
    for t in report.targets:
        out.append(f"  {t['name']:12}{_rgb(t['target']):15}{_rgb(t['today']['measured']):15}{t['today']['de']:5.1f}  "
                   f"{_rgb(t['linear']['measured']):15}{t['linear']['de']:5.1f}")

    out += ["", "For render/palette.py, if these hold up (Ink.estimated; white balancing leaves them as is):"]
    for name in INK_NAMES:
        out.append(f"  {name:7}{tuple(report.inks[name])}")
    return "\n".join(out)


def write_overlay(measured: Measured, out: str | Path, width: int = 1400) -> None:
    """The photo with every measured area outlined, to check by eye that the card was read
    in the right place."""
    linear = measured.photo.linear
    scale = min(1.0, width / linear.shape[1])
    step = max(1, int(1 / scale))
    small = (linear_to_srgb(np.clip(linear[::step, ::step], 0, 1)) * 255).astype(np.uint8)
    img = Image.fromarray(small)
    draw = ImageDraw.Draw(img)
    for patch in PATCHES:
        x0, y0, x1, y1 = patch.sample
        corners = _project(measured.homography, np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=float)) / step
        draw.polygon([tuple(p) for p in corners], outline=(255, 0, 255))
    for x, y in measured.corners:
        cx, cy = x / step, y / step
        draw.line([(cx - 12, cy), (cx + 12, cy)], fill=(0, 255, 255), width=2)
        draw.line([(cx, cy - 12), (cx, cy + 12)], fill=(0, 255, 255), width=2)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
