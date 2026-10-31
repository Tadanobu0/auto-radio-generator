"""字体の見比べ用の静止画を書き出す (ブリーフ 017)。

本番と同じ組み立て (VoicevoxClient._generate_ass の字幕、VideoTrackRenderer の日付・章タイトル・
字幕のフィルタ、ThumbnailGenerator のサムネイル) で、背景 1 枚に重ねた静止画を数枚作る。
字体は本番と同じく環境変数 ARG_FONT_DIR に従う (無ければ Meiryo)。

例 (Mac、BIZ UDPGothic):
    ARG_FONT_DIR=/Users/tada/radio-fonts python3 scripts/render_font_samples.py \\
        --ffmpeg /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg \\
        --out output/font_samples_017 \\
        --reference-dir /Users/tada/radio_director/output/renders/20261101_004228/frames

出力:
    <out>/frame_NN.png       字幕 (1 行・2 行、2 人の話者) + 右上の日付 + 章タイトル (1 行・2 行)
    <out>/thumbnail.png      サムネイル
    <out>/subtitles.ass      使った字幕
    <out>/compare_sheet.png  --reference-dir を渡したとき。左に本番のコマ、右に今回の静止画を並べる
    <out>/index.txt          各画像の中身と使った字体
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PRE_ROLL_MS = 2000  # _generate_ass が足す冒頭の無音
SLOT_MS = 10000

# (話者, 字幕の文, 章タイトル)。1 行・2 行の字幕と、1 行・2 行 (「：」で分かれる) の章タイトルを混ぜる
SAMPLES = [
    ("A", "みなさんこんにちは、ずんだもんなのだ。", "家のWi-Fiが遅い本当の理由"),
    (
        "B",
        "ルーターを床に直置きすると、電波は家具や壁にさえぎられて、2階まで届きにくくなりますの。",
        "家のHDDに当てはめる：1台の確率と前ぶれと備え",
    ),
    (
        "A",
        "腰の高さの棚に置き直しただけで、速度が倍になった例もあるのだ。40%の改善なのだ！",
        "電波はどこで弱まる？ 距離・壁・水と金属",
    ),
    ("B", "小さく始めて、記録を見ながら広げる。それがいちばん確かな近道ですわね。", "まとめ"),
]
THUMBNAIL_TITLE = "Wi-Fiが遅いのはルーターの置き場所だった"


def _phrase_data():
    return [
        (None, i * SLOT_MS, i * SLOT_MS + SLOT_MS - 2000, text, speaker)
        for i, (speaker, text, _title) in enumerate(SAMPLES)
    ]


def _chapters():
    from core.interfaces import ChapterMarker

    return [
        ChapterMarker(start_time_sec=(i * SLOT_MS + PRE_ROLL_MS) / 1000.0, title=title, section_id=f"sample_{i}")
        for i, (_speaker, _text, title) in enumerate(SAMPLES)
    ]


def _write_ass(out_dir: Path) -> Path:
    from services.audio_synthesis.voicevox_client import VoicevoxClient

    path = out_dir / "subtitles.ass"
    VoicevoxClient.__new__(VoicevoxClient)._generate_ass(_phrase_data(), path)
    return path


def _render_frames(ffmpeg: str, background: Path, ass_path: Path, out_dir: Path) -> list[Path]:
    from services.video_rendering.video_track_renderer import VideoTrackRenderer

    renderer = VideoTrackRenderer.__new__(VideoTrackRenderer)
    total_sec = (len(SAMPLES) * SLOT_MS + PRE_ROLL_MS) / 1000.0
    overlays = [
        renderer._build_date_filter(),
        renderer._build_topic_overlay_filter(_chapters(), total_sec),
        renderer._build_subtitle_filter(ass_path),
    ]
    graph = (
        "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
        + ",".join(f for f in overlays if f)
        + "[v]"
    )
    frames = []
    for i in range(len(SAMPLES)):
        # 各字幕の中ほど (発話の始まりから 3 秒後) のコマ
        t = (i * SLOT_MS + PRE_ROLL_MS + 3000) / 1000.0
        out = out_dir / f"frame_{i + 1:02d}.png"
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-framerate", "5", "-i", str(background),
            "-filter_complex", graph, "-map", "[v]",
            "-ss", f"{t:.3f}", "-frames:v", "1", str(out),
        ]
        subprocess.run(cmd, check=True)
        frames.append(out)
        print(f"wrote {out}")
    return frames


def _render_thumbnail(background: Path, out_dir: Path) -> Path:
    from services.media_processing.thumbnail_generator import ThumbnailGenerator

    out = out_dir / "thumbnail.png"
    ThumbnailGenerator().generate(
        title=THUMBNAIL_TITLE, background_path=background, output_path=out, thumbnail_title=THUMBNAIL_TITLE
    )
    return out


def _compare_sheet(reference_dir: Path, images: list[Path], out: Path) -> Path | None:
    """左に本番のコマ (reference_dir の画像を名前順に)、右に今回の画像を 960x540 で並べる。"""
    from PIL import Image, ImageDraw

    refs = sorted(p for p in reference_dir.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg"))
    if not refs:
        print(f"no reference frames in {reference_dir}")
        return None
    rows = max(len(refs[:len(images)]), len(images))
    cell_w, cell_h, label_h = 960, 540, 36
    sheet = Image.new("RGB", (cell_w * 2, (cell_h + label_h) * rows), "black")
    draw = ImageDraw.Draw(sheet)
    for row in range(rows):
        y = row * (cell_h + label_h)
        for col, path in enumerate((refs[row] if row < len(refs) else None, images[row] if row < len(images) else None)):
            if path is None:
                continue
            with Image.open(path) as img:
                img = img.convert("RGB")
                img.thumbnail((cell_w, cell_h))
                sheet.paste(img, (col * cell_w, y + label_h))
            draw.text((col * cell_w + 8, y + 8), f"{'reference (Meiryo)' if col == 0 else 'this run'}: {path.name}", fill="white")
    sheet.save(out)
    print(f"wrote {out}")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    parser.add_argument("--out", type=Path, default=ROOT / "output" / "font_samples_017")
    parser.add_argument("--background", type=Path, default=ROOT / "assets" / "backgrounds" / "default.png")
    parser.add_argument("--reference-dir", type=Path, default=None, help="本番 (Meiryo) のコマのフォルダ")
    args = parser.parse_args(argv)

    from services import render_fonts

    font = render_fonts.active_font()
    args.out.mkdir(parents=True, exist_ok=True)
    ass_path = _write_ass(args.out)
    frames = _render_frames(args.ffmpeg, args.background, ass_path, args.out)
    thumbnail = _render_thumbnail(args.background, args.out)

    lines = [
        f"font: {font.name} (regular={font.regular}, bold={font.bold}, fonts_dir={font.fonts_dir})",
        f"ASS Fontsize: {font.ass_font_size(70)} (em 70px)",
        f"background: {args.background}",
        "",
    ]
    for frame, (speaker, text, title) in zip(frames, SAMPLES):
        lines.append(f"{frame.name}: speaker={speaker} title={title!r} text={text!r}")
    lines.append(f"{thumbnail.name}: {THUMBNAIL_TITLE!r}")
    if args.reference_dir is not None:
        sheet = _compare_sheet(args.reference_dir.expanduser(), frames + [thumbnail], args.out / "compare_sheet.png")
        if sheet is not None:
            lines.append(f"{sheet.name}: left = {args.reference_dir}, right = this run")
    (args.out / "index.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out / 'index.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
