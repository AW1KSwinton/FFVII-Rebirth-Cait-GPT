from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from .subprocess_options import no_console_window_kwargs
from typing import Mapping


class CaptureError(RuntimeError):
    pass


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
PW_RENDERFULLCONTENT = 0x00000002
# The game's loading screen also yields this: PrintWindow sees no colour.
BLANK_BACKGROUND_CAPTURE = "background PrintWindow did not return a complete game frame"


def _has_color_pixels(pixels) -> bool:
    # BGRA's alpha bytes can be nonzero even when PrintWindow supplies no
    # rendered image. Only the three color channels prove image content.
    data = bytes(pixels)
    return any(data[0::4]) or any(data[1::4]) or any(data[2::4])


def capture_obs_game_source(process_ids: Mapping[int, str], output_dir: Path, *, sequence: int | None = None) -> dict[str, object]:
    """Capture an OBS source bound to the sole allow-listed game process."""
    # The normal launcher (ff7rebirth.exe) remains alive alongside the actual
    # render process. OBS's verified source names the latter explicitly.
    render_process_ids = {pid: name for pid, name in process_ids.items() if name.lower() == "ff7rebirth_.exe"}
    if len(render_process_ids) != 1:
        raise CaptureError("OBS capture requires exactly one allow-listed game process")
    node = shutil.which("node")
    if not node:
        raise CaptureError("OBS game-source capture requires the existing Node runtime")
    now = datetime.now(timezone.utc)
    output_dir.mkdir(parents=True, exist_ok=True)
    image_path = (output_dir / f"ff7r-obs-{now.strftime('%Y%m%dT%H%M%S.%fZ')}.png").resolve()
    try:
        response = subprocess.run(
            [node, str(Path(__file__).with_name("obs_game_capture.mjs")), str(image_path)],
            capture_output=True, text=True, timeout=20,
            **no_console_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaptureError("OBS game-source capture did not complete within its bounded request") from exc
    if response.returncode != 0:
        raise CaptureError("OBS refused the game-source capture: " + response.stderr.strip()[-600:])
    try:
        result = json.loads(response.stdout)
        if result.get("ok") is not True or result.get("image_path") != str(image_path):
            raise ValueError("capture output identity differs")
        data = image_path.read_bytes()
        if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
            raise ValueError("capture image is not PNG")
        width, height = struct.unpack_from(">II", data, 16)
        if width != result.get("width") or height != result.get("height") or not (0 < width <= 4096 and 0 < height <= 4096):
            raise ValueError("capture image dimensions differ")
    except (ValueError, OSError) as exc:
        raise CaptureError("OBS game-source capture returned invalid evidence") from exc
    metadata = {
        **result, "captured_at_utc": now.isoformat().replace("+00:00", "Z"),
        "capture_method": "obs_game_source", "background_capture": True,
        "pid": next(iter(render_process_ids)), "process_name": "ff7rebirth_.exe",
        "sequence": sequence, "sha256": hashlib.sha256(data).hexdigest(),
        "startup_frame": {"label": "unknown", "classification_basis": "obs_source_not_semantically_classified"},
    }
    image_path.with_suffix(".json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "capture"


def _write_bmp(path: Path, width: int, height: int, pixels: bytes) -> None:
    expected = width * height * 4
    if len(pixels) != expected:
        raise CaptureError(f"unexpected pixel buffer size: {len(pixels)} != {expected}")
    info_offset = 14 + 40
    file_size = info_offset + len(pixels)
    path.write_bytes(
        struct.pack("<2sIHHI", b"BM", file_size, 0, 0, info_offset)
        + struct.pack(
            "<IiiHHIIiiII",
            40,
            width,
            -height,
            1,
            32,
            0,
            len(pixels),
            0,
            0,
            0,
            0,
        )
        + pixels
    )


def classify_startup_frame(image_path: str | Path) -> dict[str, object]:
    """Conservatively classify startup/menu/loading/gameplay evidence.

    This is deliberately a small boundary detector, not a general vision
    system.  Unknown frames remain unknown; in particular, a black frame is
    treated as loading/performance evidence rather than permission to inject
    a key press. The gameplay branch is intentionally narrow: it requires a
    structured, bright scene across the whole frame, both side regions, and
    the central/logo region, while rejecting saturated all-white ambiguity.
    """
    path = Path(image_path)
    try:
        data = path.read_bytes()
        if data[:2] != b"BM":
            raise CaptureError("startup frame is not a BMP")
        pixel_offset = struct.unpack_from("<I", data, 10)[0]
        width, signed_height = struct.unpack_from("<ii", data, 18)
        bit_count = struct.unpack_from("<H", data, 28)[0]
        height = abs(signed_height)
        if width < 16 or height < 16 or bit_count != 32:
            raise CaptureError("startup frame has unsupported BMP layout")
        pixels = data[pixel_offset:]
        expected = width * height * 4
        if len(pixels) < expected:
            raise CaptureError("startup frame pixel buffer is truncated")

        def pixel_rgb(x: int, y: int) -> tuple[int, int, int]:
            index = y * width * 4 + x * 4
            blue, green, red = pixels[index:index + 3]
            return int(red), int(green), int(blue)

        def region_nonblack(left: float, top: float, right: float, bottom: float) -> float:
            x0, x1 = int(width * left), max(int(width * right), int(width * left) + 1)
            y0, y1 = int(height * top), max(int(height * bottom), int(height * top) + 1)
            step_x = max(1, (x1 - x0) // 32)
            step_y = max(1, (y1 - y0) // 18)
            sampled = nonblack = 0
            for y in range(y0, min(y1, height), step_y):
                row = y * width * 4
                for x in range(x0, min(x1, width), step_x):
                    index = row + x * 4
                    blue, green, red = pixels[index:index + 3]
                    sampled += 1
                    if (int(blue) + int(green) + int(red)) / 3.0 > 20.0:
                        nonblack += 1
            return nonblack / sampled if sampled else 0.0

        # Regional nonblack fractions are useful boundary evidence, but they
        # saturate on a bright, detailed gameplay frame just as they do on a
        # uniform white capture.  Add a small fixed-grid structure test so the
        # positive gameplay branch requires actual luminance variation and
        # adjacent-sample edges, not merely "not black" pixels.
        sample_width, sample_height = 64, 36
        luminances: list[float] = []
        grid: list[list[float]] = []
        for grid_y in range(sample_height):
            y = min(height - 1, int((grid_y + 0.5) * height / sample_height))
            row: list[float] = []
            for grid_x in range(sample_width):
                x = min(width - 1, int((grid_x + 0.5) * width / sample_width))
                red, green, blue = pixel_rgb(x, y)
                # Rec. 709 luminance keeps the structure metric sensitive to
                # colour and shading while remaining deterministic and cheap.
                luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0
                row.append(luminance)
                luminances.append(luminance)
            grid.append(row)
        luminance_mean = sum(luminances) / len(luminances) if luminances else 0.0
        luminance_variance = (
            sum((value - luminance_mean) ** 2 for value in luminances) / len(luminances)
            if luminances else 0.0
        )
        edge_differences = [
            abs(grid[y][x] - grid[y][x - 1])
            for y in range(sample_height)
            for x in range(1, sample_width)
        ] + [
            abs(grid[y][x] - grid[y - 1][x])
            for y in range(1, sample_height)
            for x in range(sample_width)
        ]
        edge_fraction = (
            sum(1 for difference in edge_differences if difference >= 0.08) / len(edge_differences)
            if edge_differences else 0.0
        )

        # The loading screen is black apart from a small blue "Loading" label
        # and spinner in the bottom-right corner (live 2026-10-14: x .86-.94,
        # y .83-.89). Telemetry can report a loaded world while it is still
        # up, so recognise it positively instead of inferring it from overall
        # darkness, which a dim scene shares.
        corner_left, corner_top, corner_right, corner_bottom = 0.78, 0.74, 0.99, 0.97
        cx0, cx1 = int(width * corner_left), int(width * corner_right)
        cy0, cy1 = int(height * corner_top), int(height * corner_bottom)
        corner_step = max(1, width // 768)
        corner_sampled = corner_lit = corner_blue = 0
        for y in range(cy0, cy1, corner_step):
            for x in range(cx0, cx1, corner_step):
                red, green, blue = pixel_rgb(x, y)
                corner_sampled += 1
                if (red + green + blue) / 3.0 > 20.0:
                    corner_lit += 1
                    if blue > red + 20 and blue >= green:
                        corner_blue += 1
        outside_step = max(1, width // 128)
        outside_sampled = outside_lit = 0
        for y in range(0, height, outside_step):
            for x in range(0, width, outside_step):
                if cx0 <= x < cx1 and cy0 <= y < cy1:
                    continue
                red, green, blue = pixel_rgb(x, y)
                outside_sampled += 1
                if (red + green + blue) / 3.0 > 20.0:
                    outside_lit += 1

        metrics = {
            "whole_nonblack": region_nonblack(0.0, 0.045, 1.0, 1.0),
            "outside_loading_corner_nonblack": outside_lit / outside_sampled if outside_sampled else 0.0,
            "loading_corner_lit": corner_lit / corner_sampled if corner_sampled else 0.0,
            "loading_corner_blue_share": corner_blue / corner_lit if corner_lit else 0.0,
            "left_menu_nonblack": region_nonblack(0.026, 0.477, 0.452, 1.0),
            "right_background_nonblack": region_nonblack(0.452, 0.11, 0.994, 1.0),
            "logo_nonblack": region_nonblack(0.32, 0.27, 0.90, 0.82),
            "title_prompt_nonblack": region_nonblack(0.32, 0.86, 0.87, 1.0),
            "luminance_mean": luminance_mean,
            "luminance_stddev": luminance_variance ** 0.5,
            "edge_fraction": edge_fraction,
        }
        if (
            metrics["outside_loading_corner_nonblack"] <= 0.005
            and 0.002 <= metrics["loading_corner_lit"] <= 0.25
            and metrics["loading_corner_blue_share"] >= 0.80
        ):
            label = "loading"
        elif (
            metrics["whole_nonblack"] >= 0.30
            and metrics["left_menu_nonblack"] >= 0.07
            # Gameplay scenes can be bright across the whole frame.  The
            # title/menu layout has a comparatively dark left-side menu
            # column; keep this boundary conservative so gameplay is not
            # mistaken for the main menu.
            and metrics["left_menu_nonblack"] <= 0.60
            and metrics["right_background_nonblack"] >= 0.55
        ):
            label = "main_menu"
        elif metrics["logo_nonblack"] >= 0.15 and metrics["right_background_nonblack"] < 0.55:
            label = "title_prompt"
        elif (
            # Historical active-gameplay evidence was bright across the
            # whole frame (0.876), left side (0.930), right background
            # (0.898), and logo region (0.890). Require all of those regions
            # to be bright and non-saturated. The upper bounds deliberately
            # keep a fully white/ambiguous frame as unknown.
            0.75 <= metrics["whole_nonblack"] < 0.98
            and 0.75 <= metrics["left_menu_nonblack"] < 0.98
            and 0.75 <= metrics["right_background_nonblack"] < 0.98
            and 0.50 <= metrics["logo_nonblack"] < 0.98
            and 0.50 <= metrics["title_prompt_nonblack"] < 0.98
        ):
            label = "gameplay"
        elif (
            # Attempt-005 was a genuinely rendered world frame whose
            # regional nonblack measurements saturated at 1.0.  This second
            # positive branch accepts that case only when the full frame has
            # a restrained luminance range, substantial variation, and a
            # coarse edge signal.  Uniform bright/white ambiguity therefore
            # remains unknown, and the dark left menu column still prevents
            # a typical main-menu frame from entering this branch.
            metrics["whole_nonblack"] >= 0.75
            and metrics["left_menu_nonblack"] >= 0.75
            and metrics["right_background_nonblack"] >= 0.75
            and metrics["logo_nonblack"] >= 0.75
            and metrics["title_prompt_nonblack"] >= 0.75
            and 0.20 <= metrics["luminance_mean"] <= 0.80
            and metrics["luminance_stddev"] >= 0.12
            and metrics["edge_fraction"] >= 0.12
        ):
            label = "gameplay"
        else:
            label = "unknown"
        return {"label": label, "metrics": metrics,
                "classification_basis": "regional_brightness_heuristic"}
    except (OSError, struct.error, ValueError, CaptureError) as exc:
        return {"label": "unknown", "error": str(exc)}


def capture_foreground_game_window(
    process_ids: Mapping[int, str],
    output_dir: Path,
    prefix: str = "ff7r",
    sequence: int | None = None,
    *,
    background_hwnd: int | None = None,
) -> dict[str, object]:
    """Capture an allow-listed game window without changing foreground focus.

    Without ``background_hwnd`` this captures the foreground window. When a
    specific background render window is supplied, only PrintWindow is
    accepted; a screen-copy fallback could capture another foreground app.
    """
    if os.name != "nt":
        raise CaptureError("game-window capture requires Windows")
    if not process_ids:
        raise CaptureError("FFVII Rebirth process was not found")
    if background_hwnd is not None and (type(background_hwnd) is not int or background_hwnd <= 0):
        raise CaptureError("background game window handle is invalid")

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    handle = ctypes.c_void_p
    user32.GetForegroundWindow.restype = handle
    user32.GetWindowThreadProcessId.argtypes = [handle, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowRect.argtypes = [handle, ctypes.POINTER(RECT)]
    user32.GetDC.argtypes = [handle]
    user32.GetDC.restype = handle
    user32.ReleaseDC.argtypes = [handle, handle]
    user32.PrintWindow.argtypes = [handle, handle, ctypes.c_uint]
    gdi32.CreateCompatibleDC.argtypes = [handle]
    gdi32.CreateCompatibleDC.restype = handle
    gdi32.CreateCompatibleBitmap.argtypes = [handle, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = handle
    gdi32.SelectObject.argtypes = [handle, handle]
    gdi32.SelectObject.restype = handle
    gdi32.DeleteObject.argtypes = [handle]
    gdi32.DeleteDC.argtypes = [handle]
    gdi32.BitBlt.argtypes = [handle, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, handle, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    gdi32.GetDIBits.argtypes = [handle, handle, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), ctypes.c_uint]
    hwnd_raw = background_hwnd if background_hwnd is not None else user32.GetForegroundWindow()
    hwnd = int(hwnd_raw.value if hasattr(hwnd_raw, "value") else hwnd_raw or 0)
    if not hwnd:
        raise CaptureError("foreground game window handle is unavailable")
    process_id = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    pid = int(process_id.value)
    if pid not in process_ids:
        window_label = "background render window" if background_hwnd is not None else "foreground window"
        raise CaptureError(f"{window_label} is not the allow-listed FFVII Rebirth process")
    if background_hwnd is not None:
        user32.IsIconic.argtypes = [handle]
        if user32.IsIconic(hwnd):
            raise CaptureError("background capture requires an unminimized game window")

    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise CaptureError("GetWindowRect failed")
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if not 16 <= width <= 16384 or not 16 <= height <= 16384:
        raise CaptureError(f"invalid game window dimensions: {width}x{height}")

    window_dc = user32.GetDC(hwnd)
    if not window_dc:
        raise CaptureError("GetDC failed for the game window")
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    if not memory_dc or not bitmap:
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)
        raise CaptureError("could not allocate a compatible capture surface")

    old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
    mode = "print_window"
    try:
        rendered = bool(user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT))
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        pixels = (ctypes.c_ubyte * (width * height * 4))()
        copied = int(
            gdi32.GetDIBits(
                memory_dc,
                bitmap,
                0,
                height,
                ctypes.byref(pixels),
                ctypes.byref(info),
                DIB_RGB_COLORS,
            )
        )
        has_color = _has_color_pixels(pixels)
        if background_hwnd is not None and (not rendered or copied != height or not has_color):
            raise CaptureError(BLANK_BACKGROUND_CAPTURE)
        if not rendered or copied != height or not has_color:
            mode = "bitblt_fallback"
            if user32.GetForegroundWindow() != hwnd:
                raise CaptureError("foreground game window changed before screen-copy fallback")
            if not gdi32.BitBlt(
                memory_dc,
                0,
                0,
                width,
                height,
                window_dc,
                0,
                0,
                SRCCOPY | CAPTUREBLT,
            ):
                raise CaptureError(
                    f"PrintWindow/BitBlt failed (PrintWindow={rendered}, GetDIBits={copied})"
                )
            copied = int(
                gdi32.GetDIBits(
                    memory_dc,
                    bitmap,
                    0,
                    height,
                    ctypes.byref(pixels),
                    ctypes.byref(info),
                    DIB_RGB_COLORS,
                )
            )
        if copied != height:
            raise CaptureError("GetDIBits did not return a complete game frame")
        data = bytes(pixels)
    finally:
        gdi32.SelectObject(memory_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
    sequence_part = f"-seq{int(sequence)}" if sequence is not None else ""
    output_dir.mkdir(parents=True, exist_ok=True)
    image_path = output_dir / f"{_safe_name(prefix)}-{stamp}{sequence_part}.bmp"
    _write_bmp(image_path, width, height, data)
    digest = hashlib.sha256(data).hexdigest()
    metadata = {
        "captured_at_utc": now.isoformat().replace("+00:00", "Z"),
        "capture_method": mode,
        "background_capture": background_hwnd is not None,
        "hwnd": hwnd,
        "pid": pid,
        "process_name": process_ids[pid],
        "rect": {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom},
        "width": width,
        "height": height,
        "sequence": sequence,
        "image_path": str(image_path),
        "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
        "pixel_sha256": digest,
    }
    metadata["startup_frame"] = classify_startup_frame(image_path)
    image_path.with_suffix(".json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return {"ok": True, **metadata}
