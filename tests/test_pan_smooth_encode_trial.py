"""滑らかなパン (010) の試しエンコード。普段は skip。

    pytest -q -s -m encode_trial tests/test_pan_smooth_encode_trial.py

本番と同じ VideoTrackRenderer のコマンドで 1 場面 (90 秒) をエンコードし、
  - 点を 1 つ置いた画像で、コマごとの移動量を 009 (crop) と 010 (perspective) で比べる
  - 素材画像で、両方式のコマ (0 / 30 / 60 / 89 秒) を書き出す
  - 1 分の映像トラックの所要時間とサイズを 静止画 / crop / perspective で比べる
  - 長い章タイトル 2 つと日付が入ったコマを書き出す
コマと数字は試験 clone の output/trial_010/ に残す (git の管理外)。
"""
import json
import subprocess
import time
from pathlib import Path

import pytest

from core.interfaces import ChapterMarker
from core.models import SceneTimelineEntry, SegmentTimelineEntry, VideoTimeline
from core.models.config import VideoRendererConfig
from services.video_rendering.video_track_renderer import VideoTrackRenderer
from tests.pan_measure import format_stats, step_stats, track_blob, write_blob_card
from tests.test_video_track_pan_encode_trial import TRIAL_IMAGES, _find_ffmpeg, _flux_sized_image, _write_ass

pytestmark = pytest.mark.encode_trial

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "output" / "trial_010"
SCENE_SEC = 90.0
SPEED_SEC = 60.0
FRAME_TIMES = (0.0, 30.0, 60.0, 89.0)
ROI = (720, 400, 600, 340)
LONG_TITLES = [
    "電波はどこで弱まる？ 距離・壁・水と金属",
    "まとめ: 仕組みを知れば置き場所は変えられる",
]


@pytest.fixture(autouse=True)
def _only_when_selected(request):
    if "encode_trial" not in (request.config.getoption("markexpr") or ""):
        pytest.skip("試しエンコードは -m encode_trial のときだけ流す")


@pytest.fixture(scope="module")
def out_dir() -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR


def _timeline(scenes: list[tuple[Path, float]], subtitle_path=None) -> VideoTimeline:
    total = sum(d for _, d in scenes)
    entries = []
    start = 0.0
    for k, (image, duration) in enumerate(scenes):
        entries.append(SceneTimelineEntry(k, k, k + 1, start, duration, image))
        start += duration
    seg = SegmentTimelineEntry(
        segment_id="deep_dive_1", segment_type="deep_dive", topic_title="試し",
        audio_start_sec=0.0, audio_end_sec=total, duration_sec=total,
        video_duration_sec=total, video_cut_time_sec=total,
        background_image_path=scenes[0][0], scenes=entries,
    )
    return VideoTimeline(
        segments=[seg], total_duration_sec=total,
        main_audio_path=Path("a.wav"), bgm_path=Path("b.wav"),
        bgm_volume=0.1, fade_in_sec=1.0, fade_out_sec=1.0,
        resolution="1920x1080", fps=30, subtitle_path=subtitle_path,
    )


def _encode(config, ffmpeg, timeline, out: Path, method: str, chapters=None, **settings) -> dict:
    vc = config.yaml.video_renderer
    vc.enable_pan = method != "static"
    vc.pan_method = method if method != "static" else "perspective"
    vc.video_track_use_gpu = False
    defaults = VideoRendererConfig()
    vc.pan_work_width = defaults.pan_work_width
    vc.video_track_x264_preset = defaults.video_track_x264_preset
    for key, value in settings.items():
        setattr(vc, key, value)
    renderer = VideoTrackRenderer(config)
    renderer.ffmpeg_path = ffmpeg
    if chapters is None:
        chapters = [ChapterMarker(0.0, "加湿器の置き場所", "deep_dive_1")]
    cmd = renderer._build_ffmpeg_command(timeline, out, chapters)
    started = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True)
    elapsed = time.perf_counter() - started
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")[-800:]
    return {"method": method, "sec": elapsed, "mb": out.stat().st_size / 1024 / 1024}


def _frame(ffmpeg, video: Path, at_sec: float, out: Path) -> Path:
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-ss", f"{at_sec:.3f}", "-i", str(video), "-frames:v", "1", str(out)],
        capture_output=True, check=True,
    )
    return out


def _track(ffmpeg, video: Path) -> list[tuple[float, float]]:
    roi_w, roi_h, roi_x, roi_y = ROI
    cmd = [ffmpeg, "-v", "error", "-i", str(video), "-vf", f"crop={roi_w}:{roi_h}:{roi_x}:{roi_y}",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    return track_blob(cmd, roi_w, roi_h)


def test_motion_per_frame_crop_vs_perspective(mock_app_config, tmp_path, out_dir):
    ffmpeg = _find_ffmpeg()
    card = write_blob_card(tmp_path / "card.png")
    timeline = _timeline([(card, SCENE_SEC)])
    print()
    results = {}
    for method in ("crop", "perspective"):
        video = tmp_path / f"card_{method}.mp4"
        enc = _encode(mock_app_config, ffmpeg, timeline, video, method)
        positions = _track(ffmpeg, video)
        stats = step_stats(positions)
        results[method] = stats
        (out_dir / f"motion_{method}.json").write_text(
            json.dumps({"stats": stats, "x": [round(p[0], 4) for p in positions]}, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"PAN_TRIAL {format_stats(method, stats)} (エンコード {enc['sec']:.1f}s)")
        dx = [b[0] - a[0] for a, b in zip(positions, positions[1:])]
        print(f"PAN_TRIAL {method} 先頭 60 コマの移動量: " + " ".join(f"{d:+.3f}" for d in dx[:60]))

    crop, smooth = results["crop"], results["perspective"]
    expected = 1920 * 0.12 / (SCENE_SEC * 30)
    # crop: ほとんどのコマで止まり、ときどき跳ぶ
    assert crop["still"] > 0.8 * crop["frames"]
    # perspective: 全コマが同じだけ動く (エンコード後の映像で測って ±50% 以内)
    assert smooth["still"] == 0
    assert smooth["mean"] == pytest.approx(expected, rel=0.1)
    assert smooth["min"] > 0.5 * expected and smooth["max"] < 1.5 * expected


def test_frames_of_one_scene_on_real_image(mock_app_config, tmp_path, out_dir):
    ffmpeg = _find_ffmpeg()
    image = _flux_sized_image(ffmpeg, TRIAL_IMAGES[1], tmp_path)
    timeline = _timeline([(image, SCENE_SEC)], _write_ass(tmp_path / "trial.ass"))
    print()
    for method in ("crop", "perspective"):
        video = tmp_path / f"scene_{method}.mp4"
        enc = _encode(mock_app_config, ffmpeg, timeline, video, method)
        names = [_frame(ffmpeg, video, t, out_dir / f"scene_{method}_{int(t):02d}s.png").name for t in FRAME_TIMES]
        print(f"PAN_TRIAL frames {method}: {', '.join(names)} (90 秒 {enc['sec']:.1f}s, {enc['mb']:.2f} MB)")
    print(f"PAN_TRIAL frames dir: {out_dir}")


@pytest.mark.parametrize("image_name", TRIAL_IMAGES)
def test_encode_time_static_crop_perspective(mock_app_config, tmp_path, image_name):
    ffmpeg = _find_ffmpeg()
    image = _flux_sized_image(ffmpeg, image_name, tmp_path)
    # 1 分を 2 場面 (40 秒 + 20 秒) にして、場面の切り替えも含める
    timeline = _timeline([(image, 40.0), (image, SPEED_SEC - 40.0)], _write_ass(tmp_path / "trial.ass"))
    runs = []
    for method in ("static", "crop", "perspective", "static", "crop", "perspective"):
        runs.append(_encode(mock_app_config, ffmpeg, timeline, tmp_path / f"speed_{method}_{len(runs)}.mp4", method))
    print()
    print(f"PAN_TRIAL speed image: {image_name[:40]} (768x432), {SPEED_SEC:.0f}s, 1920x1080 30fps, 字幕・日付・章タイトルあり, libx264 ultrafast")
    base = min(r["sec"] for r in runs if r["method"] == "static")
    for method in ("static", "crop", "perspective"):
        mine = [r for r in runs if r["method"] == method]
        best = min(r["sec"] for r in mine)
        times = " / ".join("%.1fs" % r["sec"] for r in mine)
        print(
            f"PAN_TRIAL speed {method}: {times} "
            f"(速い方 {best:.1f}s = 静止画の {best / base:.2f} 倍, {SPEED_SEC / best:.1f}x realtime), "
            f"{mine[0]['mb']:.2f} MB"
        )


VARIANTS = [
    # (名前, 方式, 設定)
    ("static ultrafast", "static", {}),
    ("crop ultrafast", "crop", {}),
    ("persp1920 ultrafast", "perspective", {"pan_work_width": 0}),
    ("persp960 ultrafast", "perspective", {"pan_work_width": 960}),
    ("persp768 ultrafast", "perspective", {"pan_work_width": 768}),
    ("persp960 superfast", "perspective", {"pan_work_width": 960, "video_track_x264_preset": "superfast"}),
    ("persp768 superfast", "perspective", {"pan_work_width": 768, "video_track_x264_preset": "superfast"}),
    ("persp768 veryfast", "perspective", {"pan_work_width": 768, "video_track_x264_preset": "veryfast"}),
    ("persp960 nvenc", "perspective", {"pan_work_width": 960, "video_track_use_gpu": True}),
    ("persp768 nvenc", "perspective", {"pan_work_width": 768, "video_track_use_gpu": True}),
]
MOTION_SEC = 30.0


def test_variants_speed_size_and_motion(mock_app_config, tmp_path, out_dir):
    """作業用の幅 × エンコーダの組み合わせごとに、1 分の時間・サイズと、点の画像のコマごとの動き。"""
    ffmpeg = _find_ffmpeg()
    image = _flux_sized_image(ffmpeg, TRIAL_IMAGES[1], tmp_path)
    ass = _write_ass(tmp_path / "trial.ass")
    speed_tl = _timeline([(image, 40.0), (image, SPEED_SEC - 40.0)], ass)
    card = write_blob_card(tmp_path / "card.png")
    motion_tl = _timeline([(card, MOTION_SEC)])
    print()
    print(f"PAN_VARIANT 1 分 (40 秒 + 20 秒の 2 場面, 1920x1080 30fps, 字幕・日付・章タイトルあり, 素材 {TRIAL_IMAGES[1][:30]} 768x432)")
    rows = []
    for k, (name, method, settings) in enumerate(VARIANTS):
        try:
            times = []
            for rep in range(2):
                enc = _encode(mock_app_config, ffmpeg, speed_tl, tmp_path / f"v{k}_{rep}.mp4", method, **settings)
                times.append(enc["sec"])
            card_video = tmp_path / f"v{k}_card.mp4"
            _encode(mock_app_config, ffmpeg, motion_tl, card_video, method, **settings)
            stats = step_stats(_track(ffmpeg, card_video))
        except AssertionError as e:
            print(f"PAN_VARIANT {name}: 失敗 {str(e)[-300:]}")
            continue
        rows.append((name, min(times), enc["mb"], stats))
    base = rows[0][1]
    for name, sec, mb, s in rows:
        print(
            f"PAN_VARIANT {name}: {sec:.1f}s (静止画の {sec / base:.2f} 倍, 14 分なら {sec * 14:.0f}s), {mb:.2f} MB/分, "
            f"点の移動 平均 {s['mean']:+.4f} 標準偏差 {s['std']:.4f} 最小 {s['min']:+.4f} 最大 {s['max']:+.4f} px/コマ, "
            f"止まったコマ {s['still']}/{s['frames']}"
        )
    (out_dir / "variants.json").write_text(
        json.dumps([{"name": n, "sec": t, "mb": m, "stats": s} for n, t, m, s in rows], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def test_long_titles_with_date(mock_app_config, tmp_path, out_dir):
    ffmpeg = _find_ffmpeg()
    images = [_flux_sized_image(ffmpeg, name, tmp_path) for name in TRIAL_IMAGES]
    timeline = _timeline([(images[0], 3.0), (images[1], 3.0)])
    chapters = [ChapterMarker(0.0, LONG_TITLES[0], "deep_dive_1"), ChapterMarker(3.0, LONG_TITLES[1], "conclusion")]
    video = tmp_path / "titles.mp4"
    _encode(mock_app_config, ffmpeg, timeline, video, "perspective", chapters)
    print()
    for k, at in enumerate((1.5, 4.5)):
        out = _frame(ffmpeg, video, at, out_dir / f"title_{k + 1}.png")
        print(f"PAN_TRIAL title frame: {out.name} ({LONG_TITLES[k]})")
