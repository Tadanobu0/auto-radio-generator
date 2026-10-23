"""パンを入れた映像トラックの試しエンコード (009)。普段は skip。

    pytest -q -s -m encode_trial tests/test_video_track_pan_encode_trial.py

既存の素材画像 (assets/backgrounds) で 1 分ぶんの映像トラックを、本番と同じ
VideoTrackRenderer のコマンド (日付・章タイトル・字幕の重ね込みあり) でエンコードし、
静止画 / パン × libx264 / NVENC の所要時間とファイルサイズを出す。画像生成はしない。
"""
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from core.interfaces import ChapterMarker
from core.models import SceneTimelineEntry, SegmentTimelineEntry, VideoTimeline
from services.video_rendering.video_track_renderer import VideoTrackRenderer

pytestmark = pytest.mark.encode_trial

REPO_ROOT = Path(__file__).resolve().parents[1]
TRIAL_SEC = 60.0
SUBTITLE_EVERY_SEC = 4.0


@pytest.fixture(autouse=True)
def _only_when_selected(request):
    if "encode_trial" not in (request.config.getoption("markexpr") or ""):
        pytest.skip("試しエンコードは -m encode_trial のときだけ流す")


def _find_ffmpeg() -> str:
    candidates = [
        shutil.which("ffmpeg"),
        str(REPO_ROOT.parent / "ffmpeg.exe"),
        "E:/windsurf/ffmpeg.exe",
        str(Path.cwd().parent / "ffmpeg.exe"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    pytest.skip(f"ffmpeg が見つからない: {candidates}")


def _write_ass(path: Path) -> Path:
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Meiryo,56,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3,1,2,40,40,60,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    def ts(sec: float) -> str:
        return f"0:{int(sec // 60):02d}:{sec % 60:05.2f}"

    t = 0.0
    k = 0
    while t < TRIAL_SEC:
        end = min(t + SUBTITLE_EVERY_SEC, TRIAL_SEC)
        lines.append(f"Dialogue: 0,{ts(t)},{ts(end)},Default,,0,0,0,,試しの字幕その{k}。加湿器の置き場所の話をしています")
        t = end
        k += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return path


def _trial_timeline(subtitle_path: Path) -> VideoTimeline:
    images = sorted((REPO_ROOT / "assets" / "backgrounds").glob("*.png"))
    if not images:
        pytest.skip("assets/backgrounds に画像が無い")
    # 本番の 1 場面 (約 90 秒) の中の 1 分にあたる: 1 枚を 60 秒ゆっくり動かす
    scene = SceneTimelineEntry(0, 0, 10, 0.0, TRIAL_SEC, images[0])
    seg = SegmentTimelineEntry(
        segment_id="deep_dive_1", segment_type="deep_dive", topic_title="加湿器の置き場所",
        audio_start_sec=0.0, audio_end_sec=TRIAL_SEC, duration_sec=TRIAL_SEC,
        video_duration_sec=TRIAL_SEC, video_cut_time_sec=TRIAL_SEC,
        background_image_path=images[0], scenes=[scene],
    )
    return VideoTimeline(
        segments=[seg], total_duration_sec=TRIAL_SEC,
        main_audio_path=Path("a.wav"), bgm_path=Path("b.wav"),
        bgm_volume=0.1, fade_in_sec=1.0, fade_out_sec=1.0,
        resolution="1920x1080", fps=30, subtitle_path=subtitle_path,
    )


def _encode(config, ffmpeg: str, timeline: VideoTimeline, out: Path, pan: bool, nvenc: bool) -> dict:
    config.yaml.video_renderer.enable_pan = pan
    config.yaml.video_renderer.video_track_use_gpu = nvenc
    renderer = VideoTrackRenderer(config)
    renderer.ffmpeg_path = ffmpeg
    chapters = [ChapterMarker(0.0, "加湿器の置き場所", "deep_dive_1")]
    cmd = renderer._build_ffmpeg_command(timeline, out, chapters)
    started = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True)
    elapsed = time.perf_counter() - started
    result = {
        "pan": pan, "encoder": "h264_nvenc" if nvenc else "libx264 ultrafast",
        "ok": proc.returncode == 0, "sec": elapsed,
        "mb": out.stat().st_size / 1024 / 1024 if out.exists() else 0.0,
    }
    if proc.returncode != 0:
        result["error"] = proc.stderr.decode("utf-8", errors="replace")[-600:]
    return result


def test_encode_one_minute_static_vs_pan(mock_app_config, tmp_path):
    ffmpeg = _find_ffmpeg()
    timeline = _trial_timeline(_write_ass(tmp_path / "trial.ass"))
    version = subprocess.run([ffmpeg, "-hide_banner", "-version"], capture_output=True).stdout.decode(errors="replace").splitlines()[0]

    runs = [
        _encode(mock_app_config, ffmpeg, timeline, tmp_path / "static_x264.mp4", pan=False, nvenc=False),
        _encode(mock_app_config, ffmpeg, timeline, tmp_path / "pan_x264.mp4", pan=True, nvenc=False),
        _encode(mock_app_config, ffmpeg, timeline, tmp_path / "static_nvenc.mp4", pan=False, nvenc=True),
        _encode(mock_app_config, ffmpeg, timeline, tmp_path / "pan_nvenc.mp4", pan=True, nvenc=True),
    ]

    base = runs[0]
    print()
    print(f"ENCODE_TRIAL ffmpeg: {version}")
    print(f"ENCODE_TRIAL image: {timeline.segments[0].background_image_path.name}, {TRIAL_SEC:.0f}s, 1920x1080 30fps, 字幕・日付・章タイトルあり")
    for r in runs:
        if not r["ok"]:
            print(f"ENCODE_TRIAL pan={r['pan']} {r['encoder']}: 失敗 {r.get('error', '')}")
            continue
        print(
            f"ENCODE_TRIAL pan={r['pan']} {r['encoder']}: {r['sec']:.1f}s "
            f"({TRIAL_SEC / r['sec']:.1f}x realtime), {r['mb']:.2f} MB "
            f"(静止画 x264 比: 時間 {r['sec'] / base['sec']:.2f} 倍, サイズ {r['mb'] / base['mb']:.2f} 倍)"
            if base["ok"] and base["mb"] > 0 else
            f"ENCODE_TRIAL pan={r['pan']} {r['encoder']}: {r['sec']:.1f}s, {r['mb']:.2f} MB"
        )

    # libx264 の 2 本は必ず通ること (NVENC はドライバ次第なので数字だけ出す)
    assert runs[0]["ok"], runs[0].get("error")
    assert runs[1]["ok"], runs[1].get("error")
