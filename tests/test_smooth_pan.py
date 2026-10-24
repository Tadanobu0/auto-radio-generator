"""背景のパンを滑らかにし、動きを増やす (010) のテスト

2026-10-24 (Wi-Fi の回): 009 のパン (1.05 倍に拡大して crop の x を 96*t/D で動かす) が
カクカクして見え、動きも小さすぎて効いているか分からなかった。crop の位置は整数ピクセル
(yuv420p では 2 の倍数) にしかならず、90 秒の場面だと 1 コマ 0.036px の動きが
「何十コマも止まって 1〜2px 跳ぶ」になる。perspective で小数ピクセルずつ動かし、
ズームを足して動きを増やした。
"""
import re
import shutil
from pathlib import Path

import pytest

from core.interfaces import ChapterMarker
from core.models import SceneTimelineEntry, SegmentTimelineEntry, VideoTimeline
from core.models.config import VideoRendererConfig
from services.video_rendering.video_track_renderer import (
    VideoTrackRenderer,
    build_pan_filter,
    build_smooth_pan_filter,
)
from tests.pan_measure import CARD_BG, step_stats, track_blob, write_blob_card

W, H, FPS = 1920, 1080, 30
ZMIN, ZMAX, FULL = 1.12, 1.22, 90.0


def _chain(duration, index=0, fps=FPS, zmin=ZMIN, zmax=ZMAX, full=FULL, width=W, height=H):
    return build_smooth_pan_filter(width, height, duration, fps, index, zmin, zmax, full)


def _persp_options(chain: str) -> dict:
    body = chain.split("perspective=")[1].split(",setsar")[0]
    return dict(part.split("=", 1) for part in body.split(":"))


def _window(chain: str, frame_no: int) -> tuple[float, float, float, float]:
    """perspective の式を frame_no (1 始まり、ffmpeg の in) で評価した窓 (左, 右, 上, 下)。"""
    opts = _persp_options(chain)

    def ev(expr):
        assert re.fullmatch(r"[0-9.+\-*/() in]+", expr), expr
        return eval(expr.replace("in", str(frame_no)), {"__builtins__": {}})

    return ev(opts["x0"]), ev(opts["x1"]), ev(opts["y0"]), ev(opts["y2"])


def _centre_and_zoom(chain, frame_no, width=W):
    left, right, _top, _bottom = _window(chain, frame_no)
    zoom = width / (right - left)
    centre_out = (width / 2 - left) * zoom  # 画像の中央が出力のどこに来るか
    return centre_out, zoom


# ---------------------------------------------------------------- フィルタの形

def test_smooth_pan_uses_perspective_per_frame():
    chain = _chain(90.0)
    assert chain.startswith("scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,format=yuv420p,")
    opts = _persp_options(chain)
    assert opts["sense"] == "source" and opts["eval"] == "frame" and opts["interpolation"] == "linear"
    # 整数ピクセルにしか動けない crop / zoompan では動かさない
    assert "zoompan" not in chain and ":x=" not in chain
    # filter_complex の区切りになる ',' ';' は式の中に無い
    for key in ("x0", "y0", "x1", "y1", "x2", "y2", "x3", "y3"):
        assert "," not in opts[key] and ";" not in opts[key]


def test_window_is_a_rectangle_inside_the_image_every_frame():
    for duration, index in [(90.0, 0), (90.0, 1), (30.0, 0), (150.0, 1)]:
        chain = _chain(duration, index)
        opts = _persp_options(chain)
        assert opts["x0"] == opts["x2"] and opts["x1"] == opts["x3"]
        assert opts["y0"] == opts["y1"] and opts["y2"] == opts["y3"]
        for n in range(1, int(duration * FPS) + 1, 7):
            left, right, top, bottom = _window(chain, n)
            assert -1e-6 <= left < right <= W + 1e-6
            assert -1e-6 <= top < bottom <= H + 1e-6
            # 縦横比は保つ (ゆがめない)
            assert (right - left) / (bottom - top) == pytest.approx(W / H)


def test_full_scene_moves_twelve_percent_and_zooms():
    chain = _chain(90.0, 0)
    last = int(90 * FPS)
    c0, z0 = _centre_and_zoom(chain, 1)
    c1, z1 = _centre_and_zoom(chain, last)
    assert z0 == pytest.approx(ZMIN) and z1 == pytest.approx(ZMAX, abs=1e-3)
    # 画像の中央が画面幅の (ZMIN - 1) = 12% (230px) 動く。009 は 96px
    assert c1 - c0 == pytest.approx(W * (ZMIN - 1), abs=0.2)


def test_centre_moves_the_same_amount_every_frame():
    chain = _chain(90.0, 0)
    centres = [_centre_and_zoom(chain, n)[0] for n in range(1, 2701)]
    steps = [b - a for a, b in zip(centres, centres[1:])]
    assert max(steps) - min(steps) < 1e-6
    assert steps[0] == pytest.approx(W * (ZMIN - 1) / 2700, rel=1e-3)


def test_scenes_alternate_direction_and_zoom():
    even, odd = _chain(90.0, 0), _chain(90.0, 1)
    ce0, ze0 = _centre_and_zoom(even, 1)
    ce1, ze1 = _centre_and_zoom(even, 2700)
    co0, zo0 = _centre_and_zoom(odd, 1)
    co1, zo1 = _centre_and_zoom(odd, 2700)
    assert ce1 > ce0 and ze1 > ze0   # 偶数: ズームインしながら右へ
    assert co1 < co0 and zo1 < zo0   # 奇数: ズームアウトしながら左へ


def test_short_scene_moves_at_the_same_speed_not_faster():
    full, short = _chain(90.0, 0), _chain(20.0, 0)
    step_full = _centre_and_zoom(full, 2)[0] - _centre_and_zoom(full, 1)[0]
    step_short = _centre_and_zoom(short, 2)[0] - _centre_and_zoom(short, 1)[0]
    assert step_short == pytest.approx(step_full)
    # 20 秒の場面は 20/90 の距離だけ動く
    moved = _centre_and_zoom(short, 600)[0] - _centre_and_zoom(short, 1)[0]
    assert moved == pytest.approx(W * (ZMIN - 1) * 599 / 2700, abs=0.05)


def test_long_scene_moves_slower_within_the_range():
    chain = _chain(180.0, 0)
    c0, z0 = _centre_and_zoom(chain, 1)
    c1, z1 = _centre_and_zoom(chain, 5400)
    assert c1 - c0 == pytest.approx(W * (ZMIN - 1), abs=0.2)
    assert z1 == pytest.approx(ZMAX, abs=1e-3)


def test_zero_length_scene_is_static():
    chain = _chain(0.0, 0)
    assert _persp_options(chain)["eval"] == "init"
    assert "in" not in _persp_options(chain)["x0"]
    assert _centre_and_zoom(chain, 1) == pytest.approx((W / 2, ZMIN))


def test_other_resolution_and_fps():
    chain = _chain(60.0, 0, fps=24, width=1280, height=720)
    assert chain.startswith("scale=1280:720:")
    assert "/2160.0" in chain  # 24fps × 90 秒
    c0, _ = _centre_and_zoom(chain, 1, width=1280)
    assert c0 == pytest.approx(1280 / 2 - 1280 * (ZMIN - 1) / 2)


# ---------------------------------------------------------------- 設定と映像トラックのコマンド

def test_config_defaults_to_perspective():
    vc = VideoRendererConfig()
    assert vc.enable_pan is True
    assert vc.pan_method == "perspective"
    assert (vc.pan_zoom_min, vc.pan_zoom_max, vc.pan_full_range_sec) == (1.12, 1.22, 90.0)


def test_config_yaml_selects_perspective():
    import yaml

    data = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
    vr = data["video_renderer"]
    assert vr["pan_method"] == "perspective"
    assert (vr["pan_zoom_min"], vr["pan_zoom_max"], vr["pan_full_range_sec"]) == (1.12, 1.22, 90.0)


def _timeline(durations=(90.0, 90.0, 20.0), fps=30) -> VideoTimeline:
    scenes = [SceneTimelineEntry(k, k, k + 1, 0.0, d, Path(f"s{k}.png")) for k, d in enumerate(durations)]
    seg = SegmentTimelineEntry(
        segment_id="deep_dive_1", segment_type="deep_dive", topic_title="t",
        audio_start_sec=0.0, audio_end_sec=sum(durations), duration_sec=sum(durations),
        video_duration_sec=sum(durations), video_cut_time_sec=sum(durations),
        background_image_path=Path("s0.png"), scenes=scenes,
    )
    return VideoTimeline(
        segments=[seg], total_duration_sec=sum(durations),
        main_audio_path=Path("a.wav"), bgm_path=Path("b.wav"),
        bgm_volume=0.1, fade_in_sec=1.0, fade_out_sec=1.0, fps=fps,
    )


def _filter_complex(config, timeline=None, **settings):
    for key, value in settings.items():
        setattr(config.yaml.video_renderer, key, value)
    renderer = VideoTrackRenderer(config)
    cmd = renderer._build_ffmpeg_command(timeline or _timeline(), Path("out.mp4"), [ChapterMarker(0.0, "t", "deep_dive_1")])
    return cmd[cmd.index("-filter_complex") + 1]


def test_render_uses_smooth_pan_by_default(mock_app_config):
    fc = _filter_complex(mock_app_config)
    for i, d in enumerate((90.0, 90.0, 20.0)):
        assert f"[{i}:v]{_chain(d, i)}[v{i}]" in fc
    assert "[v0][v1][v2]concat=n=3:v=1:a=0[vbase]" in fc


def test_render_passes_zoom_settings_and_fps(mock_app_config):
    fc = _filter_complex(mock_app_config, _timeline(fps=24), pan_zoom_min=1.08, pan_zoom_max=1.15, pan_full_range_sec=60.0)
    assert _chain(90.0, 0, fps=24, zmin=1.08, zmax=1.15, full=60.0) in fc


def test_render_crop_method_keeps_009_filter(mock_app_config):
    fc = _filter_complex(mock_app_config, pan_method="crop")
    assert build_pan_filter(1920, 1080, 90.0, 0.05, 0) in fc
    assert "perspective" not in fc


def test_render_without_pan_has_no_perspective(mock_app_config):
    fc = _filter_complex(mock_app_config, enable_pan=False)
    assert "perspective" not in fc and "crop=1920:1080:x=" not in fc


def test_unknown_pan_method_falls_back_to_perspective(mock_app_config):
    assert "perspective=" in _filter_complex(mock_app_config, pan_method="zoompan")


# ---------------------------------------------------------------- ffmpeg で実際の動きを測る

def _find_ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return found
    repo = Path(__file__).resolve().parents[1]
    for candidate in (repo.parent / "ffmpeg.exe", repo.parent.parent / "ffmpeg.exe"):
        if candidate.exists():
            return str(candidate)
    return None


FFMPEG = _find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg のある環境だけ")

CLIP_SEC = 4.0
ROI = (720, 400, 600, 340)  # 画面の中央 (点が動く範囲) だけ取り出す


def _measure(chain: str, card: Path) -> dict:
    roi_w, roi_h, roi_x, roi_y = ROI
    cmd = [
        FFMPEG, "-v", "error",
        "-loop", "1", "-framerate", str(FPS), "-t", str(CLIP_SEC), "-i", str(card),
        "-filter_complex", f"[0:v]{chain},crop={roi_w}:{roi_h}:{roi_x}:{roi_y}[out]",
        "-map", "[out]", "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    return step_stats(track_blob(cmd, roi_w, roi_h))


@needs_ffmpeg
def test_crop_pan_stands_still_then_jumps(tmp_path):
    # 原因の確認: 009 の crop は 90 秒の場面で 1 コマ 0.036px のはずが、ほとんどのコマで止まり、
    # ときどき 1px 以上跳ぶ
    card = write_blob_card(tmp_path / "card.png")
    s = _measure(build_pan_filter(W, H, 90.0, 0.05, 0), card)
    assert s["frames"] == int(CLIP_SEC * FPS)
    assert s["still"] >= 0.9 * (s["frames"] - 1)
    assert s["max_abs"] >= 0.9


@needs_ffmpeg
def test_smooth_pan_moves_every_frame_by_the_same_amount(tmp_path):
    card = write_blob_card(tmp_path / "card.png")
    s = _measure(_chain(90.0, 0), card)
    expected = W * (ZMIN - 1) / (FULL * FPS)  # 0.085px / コマ
    assert s["frames"] == int(CLIP_SEC * FPS)
    assert s["still"] == 0
    assert s["mean"] == pytest.approx(expected, rel=0.1)
    assert s["min"] > 0.5 * expected and s["max"] < 1.5 * expected
    assert CARD_BG < 60  # 点の検出のしきい値 (地 + 20) の前提
