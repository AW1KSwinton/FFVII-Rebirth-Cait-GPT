from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ff7r_agent import capture as capture_module
from ff7r_agent.capture import CaptureError, _write_bmp, classify_startup_frame
from ff7r_agent.live_route_adapter import AgentServiceRenderEvidenceSource
from ff7r_agent.service import AgentService
from ff7r_agent.models import Vec3


def _write_pattern(path: Path, predicate) -> dict[str, object]:
    width = height = 100
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            value = 180 if predicate(x, y) else 0
            pixels.extend((value, value, value, 255))
    _write_bmp(path, width, height, bytes(pixels))
    return classify_startup_frame(path)


def _write_gameplay_fixture(path: Path) -> dict[str, object]:
    # A structured 80% checkerboard keeps every sampled region bright while
    # avoiding the saturated all-white ambiguity case.
    return _write_pattern(path, lambda x, y: (x + y) % 5 != 0)


def _write_saturated_structured_gameplay_fixture(path: Path) -> dict[str, object]:
    """A bright structured scene whose regional nonblack metrics all saturate."""
    width = height = 100
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            value = 30 + ((17 * x + 31 * y) % 170)
            pixels.extend((value, value, value, 255))
    _write_bmp(path, width, height, bytes(pixels))
    return classify_startup_frame(path)


def _write_main_menu_fixture(path: Path) -> dict[str, object]:
    # Bright right-side menu artwork with a comparatively dark left column.
    return _write_pattern(path, lambda x, _y: x >= 45 or x % 5 == 0)


def _write_title_prompt_fixture(path: Path) -> dict[str, object]:
    # Central/logo artwork with a dark right background and no bright bottom
    # title-prompt strip; this remains title_prompt through the existing rule.
    return _write_pattern(path, lambda x, y: 35 <= x < 81 and 25 <= y < 81)


class _RealCaptureInput:
    is_real = True


class _CaptureEnvironment:
    def game_process_ids(self):
        return {123: "ff7rebirth_.exe"}


class _CaptureState:
    source = "telemetry"
    sequence = 1
    running = True
    paused = False
    in_menu = False
    map_id = "EndGame"
    camera = None
    player = SimpleNamespace(position=Vec3(0.0, 0.0, 0.0))

    def age_seconds(self):
        return 0.0


class _CaptureTelemetry:
    def observe(self):
        return _CaptureState()


def test_write_bmp_writes_expected_header_and_pixels(tmp_path: Path):
    path = tmp_path / "frame.bmp"
    _write_bmp(path, 2, 1, bytes([1, 2, 3, 255, 4, 5, 6, 255]))
    data = path.read_bytes()
    assert data[:2] == b"BM"
    assert int.from_bytes(data[18:22], "little", signed=True) == 2
    assert int.from_bytes(data[22:26], "little", signed=True) == -1
    assert data[-8:] == bytes([1, 2, 3, 255, 4, 5, 6, 255])


def test_write_bmp_rejects_wrong_pixel_buffer_size(tmp_path: Path):
    with pytest.raises(CaptureError, match="unexpected pixel buffer size"):
        _write_bmp(tmp_path / "frame.bmp", 2, 1, b"short")


def test_classify_startup_frame_keeps_black_frame_unknown(tmp_path: Path):
    path = tmp_path / "black.bmp"
    _write_bmp(path, 100, 100, bytes(100 * 100 * 4))
    result = classify_startup_frame(path)
    assert result["label"] == "unknown"
    assert result["metrics"]["whole_nonblack"] == 0.0


def _write_loading_screen(path: Path, *, widget=(255, 150, 60), backdrop=lambda x, y: 0) -> dict[str, object]:
    """Black apart from the blue "Loading" label and spinner, bottom right.

    Live 2026-10-14 the widget spanned x .86-.94, y .83-.89 of the frame.
    ``widget`` is BGR; ``backdrop`` gives the grey level elsewhere.
    """
    width, height = 320, 180
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            in_widget = .87 <= x / width < .93 and .84 <= y / height < .88 and (x + y) % 3 == 0
            if in_widget:
                pixels.extend((*widget, 255))
            else:
                value = backdrop(x, y)
                pixels.extend((value, value, value, 255))
    _write_bmp(path, width, height, bytes(pixels))
    return classify_startup_frame(path)


def test_loading_screen_is_recognised_by_its_widget(tmp_path: Path):
    result = _write_loading_screen(tmp_path / "loading.bmp")
    assert result["label"] == "loading"
    assert result["metrics"]["outside_loading_corner_nonblack"] == 0.0
    assert result["metrics"]["loading_corner_blue_share"] == 1.0


def test_dim_scene_with_a_blue_corner_is_not_the_loading_screen(tmp_path: Path):
    # The field HUD also sits bottom right; the loading screen is black
    # everywhere else.
    result = _write_loading_screen(tmp_path / "dim.bmp", backdrop=lambda x, y: 40 if (x * 7 + y) % 5 == 0 else 8)
    assert result["label"] != "loading"


def test_white_corner_text_on_black_is_not_the_loading_widget(tmp_path: Path):
    result = _write_loading_screen(tmp_path / "white.bmp", widget=(230, 230, 230))
    assert result["label"] == "unknown"


def test_preserved_loading_screen_classifies_as_loading_if_retained():
    # Owner's capture of the live loading screen, 2026-10-14; not in Git.
    path = Path.home() / "AppData/Local/ff7r-agent/evidence/loading-screen-2026-10-14.bmp"
    if not path.exists():
        pytest.skip("preserved loading-screen capture is not present")
    assert classify_startup_frame(path)["label"] == "loading"


def test_telemetry_never_overrides_a_loading_frame(tmp_path: Path):
    path = tmp_path / "loading.bmp"
    _write_loading_screen(path)

    def _capture(_process_ids, _output_dir, sequence=None):
        return {"ok": True, "pid": 123, "captured_at_utc": "2026-10-14T00:00:00Z",
                "image_path": str(path), "startup_frame": classify_startup_frame(path)}

    service = AgentService(
        _CaptureTelemetry(),
        _RealCaptureInput(),
        environment=_CaptureEnvironment(),
        screenshot_dir=tmp_path,
        sleeper=lambda _seconds: None,
    )
    with patch("ff7r_agent.service.capture_foreground_game_window", side_effect=_capture):
        captured = service.capture_game_window()
        evidence = AgentServiceRenderEvidenceSource(service).observe(SimpleNamespace(process_id=123))
    assert captured["startup_frame"]["label"] == "loading"
    assert "telemetry_override" not in captured["startup_frame"]
    assert evidence.ready is False


def test_classify_startup_frame_does_not_call_full_bright_gameplay_main_menu(tmp_path: Path):
    path = tmp_path / "bright-gameplay.bmp"
    _write_bmp(path, 100, 100, bytes([255, 255, 255, 255]) * (100 * 100))
    result = classify_startup_frame(path)
    assert result["label"] == "unknown"


def test_classify_structured_bright_gameplay_fixture_as_gameplay(tmp_path: Path):
    result = _write_gameplay_fixture(tmp_path / "gameplay.bmp")
    assert result["label"] == "gameplay"
    metrics = result["metrics"]
    assert 0.75 <= metrics["whole_nonblack"] < 0.98
    assert 0.75 <= metrics["left_menu_nonblack"] < 0.98
    assert 0.75 <= metrics["right_background_nonblack"] < 0.98
    assert 0.50 <= metrics["logo_nonblack"] < 0.98
    assert 0.50 <= metrics["title_prompt_nonblack"] < 0.98


def test_classify_saturated_structured_gameplay_fixture_as_gameplay(tmp_path: Path):
    result = _write_saturated_structured_gameplay_fixture(tmp_path / "saturated-gameplay.bmp")
    assert result["label"] == "gameplay"
    metrics = result["metrics"]
    assert metrics["whole_nonblack"] == 1.0
    assert metrics["left_menu_nonblack"] == 1.0
    assert metrics["right_background_nonblack"] == 1.0
    assert 0.20 <= metrics["luminance_mean"] <= 0.80
    assert metrics["luminance_stddev"] >= 0.12
    assert metrics["edge_fraction"] >= 0.12


def test_real_attempt005_gameplay_frame_classifies_raw_gameplay_if_retained():
    path = Path(
        "C:/CaitGPT/fixtures/AppData/Local/ff7r-agent/screenshots/"
        "ff7r-20260818T101232.115085Z-seq1024.bmp"
    )
    if not path.is_file():
        pytest.skip("attempt-005 BMP is not retained on this machine")
    result = classify_startup_frame(path)
    assert result["label"] == "gameplay"
    metrics = result["metrics"]
    assert metrics["whole_nonblack"] == pytest.approx(0.9696969696969697)
    assert metrics["left_menu_nonblack"] == 1.0
    assert metrics["right_background_nonblack"] == 1.0
    assert metrics["logo_nonblack"] == 1.0
    assert metrics["title_prompt_nonblack"] == 1.0
    assert metrics["luminance_stddev"] >= 0.12
    assert metrics["edge_fraction"] >= 0.12


def test_classify_main_menu_fixture_never_as_gameplay(tmp_path: Path):
    result = _write_main_menu_fixture(tmp_path / "main-menu.bmp")
    assert result["label"] == "main_menu"
    assert result["label"] != "gameplay"
    assert result["metrics"]["left_menu_nonblack"] <= 0.60


def test_classify_title_prompt_fixture_never_as_gameplay(tmp_path: Path):
    result = _write_title_prompt_fixture(tmp_path / "title-prompt.bmp")
    assert result["label"] == "title_prompt"
    assert result["label"] != "gameplay"
    assert result["metrics"]["right_background_nonblack"] < 0.55


def test_ambiguous_bright_fixture_remains_unknown(tmp_path: Path):
    _write_bmp(
        tmp_path / "ambiguous-bright.bmp",
        100,
        100,
        bytes([255, 255, 255, 255]) * (100 * 100),
    )
    result = classify_startup_frame(tmp_path / "ambiguous-bright.bmp")
    assert result["label"] == "unknown"


class _FakeWin32Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


def _fake_capture_libraries(*, pid=123, print_succeeds=True, iconic=False):
    import ctypes

    calls = {"foreground": 0, "print_window": 0, "bitblt": 0}

    def write_process_id(_hwnd, pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ulong)).contents.value = pid
        return 1

    def write_rect(_hwnd, pointer):
        rect = ctypes.cast(pointer, ctypes.POINTER(capture_module.RECT)).contents
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, 16, 16
        return 1

    def print_window(*_args):
        calls["print_window"] += 1
        return print_succeeds

    def get_dibits(_dc, _bitmap, _start, height, pixels, _info, _usage):
        target = ctypes.cast(pixels, ctypes.POINTER(ctypes.c_ubyte))
        for index in range(16 * height * 4):
            target[index] = 160
        return height

    user32 = SimpleNamespace(
        GetForegroundWindow=_FakeWin32Function(lambda: calls.__setitem__("foreground", calls["foreground"] + 1) or 999),
        GetWindowThreadProcessId=_FakeWin32Function(write_process_id),
        GetWindowRect=_FakeWin32Function(write_rect),
        IsIconic=_FakeWin32Function(lambda _hwnd: iconic),
        GetDC=_FakeWin32Function(lambda _hwnd: 101),
        ReleaseDC=_FakeWin32Function(lambda *_args: 1),
        PrintWindow=_FakeWin32Function(print_window),
    )
    gdi32 = SimpleNamespace(
        CreateCompatibleDC=_FakeWin32Function(lambda _dc: 201),
        CreateCompatibleBitmap=_FakeWin32Function(lambda *_args: 202),
        SelectObject=_FakeWin32Function(lambda *_args: 203),
        DeleteObject=_FakeWin32Function(lambda *_args: 1),
        DeleteDC=_FakeWin32Function(lambda *_args: 1),
        GetDIBits=_FakeWin32Function(get_dibits),
        BitBlt=_FakeWin32Function(lambda *_args: calls.__setitem__("bitblt", calls["bitblt"] + 1) or 1),
    )
    return SimpleNamespace(user32=user32, gdi32=gdi32), calls


def _patch_fake_win32(monkeypatch, *, pid=123, print_succeeds=True, iconic=False):
    libraries, calls = _fake_capture_libraries(
        pid=pid, print_succeeds=print_succeeds, iconic=iconic,
    )
    monkeypatch.setattr(capture_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(capture_module.ctypes, "windll", libraries, raising=False)
    return calls


def test_background_capture_uses_only_the_supplied_allowlisted_printwindow(
    tmp_path: Path, monkeypatch,
):
    calls = _patch_fake_win32(monkeypatch)

    result = capture_module.capture_foreground_game_window(
        {123: "ff7rebirth_.exe"}, tmp_path, background_hwnd=900,
    )

    assert result["background_capture"] is True
    assert result["hwnd"] == 900
    assert result["capture_method"] == "print_window"
    assert calls == {"foreground": 0, "print_window": 1, "bitblt": 0}


def test_background_capture_rejects_failed_printwindow_without_screen_fallback(
    tmp_path: Path, monkeypatch,
):
    calls = _patch_fake_win32(monkeypatch, print_succeeds=False)

    with pytest.raises(CaptureError, match="background PrintWindow"):
        capture_module.capture_foreground_game_window(
            {123: "ff7rebirth_.exe"}, tmp_path, background_hwnd=900,
        )

    assert calls == {"foreground": 0, "print_window": 1, "bitblt": 0}


@pytest.mark.parametrize("background,focus_changed", [(False, False), (True, False), (False, True)])
def test_opaque_black_printwindow_requires_safe_foreground_fallback(tmp_path, monkeypatch, background, focus_changed):
    import ctypes

    libraries, calls = _fake_capture_libraries()
    def read_pixels(_dc, _bitmap, _start, height, pixels, _info, _usage):
        target = ctypes.cast(pixels, ctypes.POINTER(ctypes.c_ubyte))
        for index in range(16 * height * 4):
            target[index] = 255 if index % 4 == 3 else (160 if calls["bitblt"] else 0)
        return height
    libraries.gdi32.GetDIBits = _FakeWin32Function(read_pixels)
    if focus_changed:
        def foreground():
            calls["foreground"] += 1
            return 999 if calls["foreground"] == 1 else 777
        libraries.user32.GetForegroundWindow = _FakeWin32Function(foreground)
    monkeypatch.setattr(capture_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(capture_module.ctypes, "windll", libraries, raising=False)
    if background or focus_changed:
        with pytest.raises(CaptureError, match="background PrintWindow" if background else "foreground game window changed"):
            capture_module.capture_foreground_game_window({123: "ff7rebirth_.exe"}, tmp_path, background_hwnd=900 if background else None)
        assert calls["bitblt"] == 0
        assert not list(tmp_path.iterdir())
    else:
        result = capture_module.capture_foreground_game_window({123: "ff7rebirth_.exe"}, tmp_path)
        assert result["capture_method"] == "bitblt_fallback"
        assert calls["bitblt"] == 1
        assert result["startup_frame"]["metrics"]["whole_nonblack"] == 1


@pytest.mark.parametrize("processes", [{}, {1: "private.exe"}, {1: "ff7rebirth_.exe", 2: "ff7rebirth_.exe"}])
def test_obs_capture_rejects_unproven_process_identity_before_starting_node(tmp_path, monkeypatch, processes):
    monkeypatch.setattr(capture_module.subprocess, "run", lambda *args, **kwargs: pytest.fail("Unproven process must not reach OBS"))
    with pytest.raises(CaptureError, match="exactly one allow-listed"):
        capture_module.capture_obs_game_source(processes, tmp_path)


@pytest.mark.parametrize("processes", [{123: "ff7rebirth_.exe"}, {122: "ff7rebirth.exe", 123: "ff7rebirth_.exe"}])
def test_obs_capture_validates_and_preserves_the_returned_image(tmp_path, monkeypatch, processes):
    import json
    import struct
    monkeypatch.setattr(capture_module.shutil, "which", lambda name: "node")
    def run(args, **kwargs):
        assert kwargs["timeout"] == 20
        output = Path(args[-1])
        data = b"\x89PNG\r\n\x1a\n" + bytes(8) + struct.pack(">II", 1536, 864)
        output.write_bytes(data)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "image_path": str(output), "width": 1536, "height": 864, "source_name": "Game"}), stderr="")
    monkeypatch.setattr(capture_module.subprocess, "run", run)
    result = capture_module.capture_obs_game_source(processes, tmp_path, sequence=42)
    assert result["capture_method"] == "obs_game_source"
    assert result["sequence"] == 42
    assert result["pid"] == 123
    assert Path(result["image_path"]).exists()
    assert Path(result["image_path"]).with_suffix(".json").exists()


def test_production_classifier_capture_override_and_adapter_contract(tmp_path: Path):
    gameplay_path = tmp_path / "gameplay.bmp"
    gameplay_result = _write_gameplay_fixture(gameplay_path)
    assert gameplay_result["label"] == "gameplay"

    unknown_path = tmp_path / "unknown.bmp"
    _write_bmp(
        unknown_path,
        100,
        100,
        bytes([255, 255, 255, 255]) * (100 * 100),
    )
    unknown_result = classify_startup_frame(unknown_path)
    assert unknown_result["label"] == "unknown"

    def capture_fixture(path: Path):
        def _capture(_process_ids, _output_dir, sequence=None):
            raw = classify_startup_frame(path)
            return {
                "ok": True,
                "pid": 123,
                "captured_at_utc": "2026-08-18T00:00:00Z",
                "image_path": str(path),
                "startup_frame": raw,
            }
        return _capture

    service = AgentService(
        _CaptureTelemetry(),
        _RealCaptureInput(),
        environment=_CaptureEnvironment(),
        screenshot_dir=tmp_path,
        sleeper=lambda _seconds: None,
    )
    observation = SimpleNamespace(process_id=123)
    with patch("ff7r_agent.service.capture_foreground_game_window", side_effect=capture_fixture(gameplay_path)):
        captured = service.capture_game_window()
        evidence = AgentServiceRenderEvidenceSource(service).observe(observation)
    assert captured["startup_frame"]["label"] == "gameplay"
    assert captured["startup_frame"]["render_classifier_label"] == "gameplay"
    assert evidence.ready is True
    assert evidence.classifier_label == "gameplay"
    assert evidence.capture["pid"] == 123

    with patch("ff7r_agent.service.capture_foreground_game_window", side_effect=capture_fixture(unknown_path)):
        overridden = service.capture_game_window()
        unknown_evidence = AgentServiceRenderEvidenceSource(service).observe(observation)
    assert overridden["startup_frame"]["label"] == "gameplay"
    assert overridden["startup_frame"]["render_classifier_label"] == "unknown"
    assert unknown_evidence.ready is False
    assert unknown_evidence.classifier_label == "unknown"
