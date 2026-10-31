"""フォントの受け口 ARG_FONT_DIR (ブリーフ 017) のテスト

- ARG_FONT_DIR が無ければ、字幕・章タイトル・日付・サムネイルとも今までどおり Meiryo
- ARG_FONT_DIR があれば、そのフォルダの BIZUDPGothic-Regular.ttf / -Bold.ttf を 4 か所で使う
- 字の大きさは em の px が正。ASS の Fontsize は winAscent + winDescent から出す (Meiryo 70px → 105)
- 字幕は 1 行ずつ別の Dialogue にし (L2 方式)、Meiryo のときと同じ行の位置に置く

本物の BIZ UDPGothic を使うテストは、ARG_TEST_FONT_DIR か ~/radio-fonts にファイルがあるときだけ動く。
ほかは寸法のテーブルだけを持つ小さな偽のフォントファイルで確かめる (Pillow では読めないので幅は概算になる)。
"""
from __future__ import annotations

import importlib
import os
import re
import struct
from pathlib import Path

import pytest

from services import render_fonts as rf
from services.audio_synthesis import subtitle_layout as sl

ROOT = Path(__file__).resolve().parent.parent

# 偽のフォントの寸法 (行の高さ 1.0em。BIZ UDPGothic と同じく行送りが詰まる字体のつもり)
FAKE_UPEM = 2048
FAKE_WIN_ASCENT = 1802
FAKE_WIN_DESCENT = 246
FAKE_FAMILY = "BIZ UDPGothic"

PHRASES = [
    (None, 0, 3000, "みなさんこんにちは、ずんだもんなのだ。", "A"),
    (
        None, 3500, 21500,
        "別のエージェントを比べた記事の結びが、今の位置をよく表していますわ。"
        "いずれも完全自律にはまだ遠いが、部分的な自律は確実に実用レベルに達している、というものですの。",
        "B",
    ),
]


def _sfnt_tables(upem: int, ascent: int, descent: int, family: str) -> dict[bytes, bytes]:
    head = bytearray(54)
    struct.pack_into(">H", head, 18, upem)
    os2 = bytearray(96)
    struct.pack_into(">HH", os2, 74, ascent, descent)
    encoded = family.encode("utf-16-be")
    name = struct.pack(">HHH", 0, 1, 18) + struct.pack(">HHHHHH", 3, 1, 0x409, 1, len(encoded), 0) + encoded
    return {b"OS/2": bytes(os2), b"head": bytes(head), b"name": name}


def _sfnt(tables: dict[bytes, bytes], start: int = 0) -> bytes:
    """オフセット表 + テーブル。start は TTC の中に置くときの先頭位置。"""
    header_len = 12 + 16 * len(tables)
    out = struct.pack(">IHHHH", 0x00010000, len(tables), 0, 0, 0)
    body = b""
    offset = start + header_len
    for tag in sorted(tables):
        data = tables[tag]
        out += struct.pack(">4sIII", tag, 0, offset + len(body), len(data))
        body += data + b"\0" * (-len(data) % 4)
    return out + body


def write_fake_font(path: Path, upem=FAKE_UPEM, ascent=FAKE_WIN_ASCENT, descent=FAKE_WIN_DESCENT, family=FAKE_FAMILY):
    path.write_bytes(_sfnt(_sfnt_tables(upem, ascent, descent, family)))
    return path


@pytest.fixture
def font_dir(tmp_path, monkeypatch):
    directory = tmp_path / "fonts"
    directory.mkdir()
    write_fake_font(directory / rf.REGULAR_FILE_NAME)
    write_fake_font(directory / rf.BOLD_FILE_NAME)
    monkeypatch.setenv(rf.ENV_FONT_DIR, str(directory))
    monkeypatch.delenv(rf.ENV_FONT_NAME, raising=False)
    return directory


@pytest.fixture
def no_font_dir(monkeypatch):
    monkeypatch.delenv(rf.ENV_FONT_DIR, raising=False)
    monkeypatch.delenv(rf.ENV_FONT_NAME, raising=False)


def _generate_ass(tmp_path) -> str:
    from services.audio_synthesis.voicevox_client import VoicevoxClient

    client = VoicevoxClient.__new__(VoicevoxClient)
    out = tmp_path / "subtitles.ass"
    client._generate_ass(PHRASES, out)
    return out.read_text(encoding="utf-8")


def _dialogues(ass_text: str) -> list[list[str]]:
    return [line.split(",", 9) for line in ass_text.splitlines() if line.startswith("Dialogue:")]


def _renderer(module: str, cls: str):
    renderer_cls = getattr(importlib.import_module(module), cls)
    return renderer_cls.__new__(renderer_cls)


# ---------------------------------------------------------------------------
# 指定が無いとき: 今までどおり Meiryo
# ---------------------------------------------------------------------------
def test_default_is_meiryo(no_font_dir):
    font = rf.active_font()
    assert font.is_default and font.name == "Meiryo"
    assert font.fonts_dir is None
    assert sl.subtitle_font_name() == "Meiryo"
    assert sl.subtitle_font_size() == sl.FONT_SIZE == 105
    assert not sl.uses_line_per_dialogue()


def test_meiryo_em_70_is_ass_105():
    # Meiryo の winAscent + winDescent = 3072 = 1.5em。em の px を正にしても今の 105 になる
    assert rf.MEIRYO_METRICS.ass_height_em == 1.5
    assert rf.MEIRYO.ass_font_size(sl.SUBTITLE_EM_PX) == 105


def test_default_ass_is_unchanged(tmp_path, no_font_dir):
    ass = _generate_ass(tmp_path)
    assert "Style: Main,Meiryo,105," in ass
    assert "Style: Band,Meiryo,105," in ass
    assert "fontsdir" not in ass
    # 既定では 1 キュー = 字幕の Dialogue 1 つ (行は \N でつなぐ)。MarginV は 0 (スタイルの 30)
    subs = [d for d in _dialogues(ass) if d[3] != "Band"]
    bands = [d for d in _dialogues(ass) if d[3] == "Band"]
    assert len(subs) == len(bands)
    assert all(d[7] == "0" for d in subs)
    assert any("\\N" in d[9] for d in subs)


@pytest.mark.parametrize("module, cls", [
    ("services.video_rendering.ffmpeg_renderer", "FfmpegRenderer"),
    ("services.video_rendering.video_track_renderer", "VideoTrackRenderer"),
])
def test_default_drawtext_font_is_meiryo(no_font_dir, module, cls):
    assert _renderer(module, cls)._get_drawtext_fontfile().endswith("meiryo.ttc")


def test_default_subtitle_filter_has_no_fontsdir(tmp_path, no_font_dir):
    renderer = _renderer("services.video_rendering.video_track_renderer", "VideoTrackRenderer")
    assert "fontsdir" not in renderer._build_subtitle_filter(tmp_path / "subtitles.ass")


def test_default_thumbnail_fonts_are_windows_fonts(no_font_dir):
    from services.media_processing.thumbnail_generator import ThumbnailGenerator

    paths = ThumbnailGenerator()._find_japanese_fonts()
    assert all(str(p).startswith("C:") for p in paths)


# ---------------------------------------------------------------------------
# フォントの寸法を読む
# ---------------------------------------------------------------------------
def test_read_font_metrics(tmp_path):
    metrics = rf.read_font_metrics(write_fake_font(tmp_path / "a.ttf"))
    assert (metrics.units_per_em, metrics.win_ascent, metrics.win_descent) == (
        FAKE_UPEM, FAKE_WIN_ASCENT, FAKE_WIN_DESCENT
    )
    assert metrics.family_name == FAKE_FAMILY
    assert metrics.ass_height_em == 1.0


def test_read_font_metrics_from_ttc(tmp_path):
    # TTC (meiryo.ttc の形) は 2 番目の字体も読める
    first = _sfnt(_sfnt_tables(2048, 2171, 901, "Meiryo"), start=20)
    second = _sfnt(_sfnt_tables(1000, 880, 120, "Meiryo UI"), start=20 + len(first))
    ttc = struct.pack(">4sIIII", b"ttcf", 0x00010000, 2, 20, 20 + len(first)) + first + second
    path = tmp_path / "x.ttc"
    path.write_bytes(ttc)
    assert rf.read_font_metrics(path) == rf.MEIRYO_METRICS
    assert rf.read_font_metrics(path, index=1).family_name == "Meiryo UI"
    assert rf.read_font_metrics(path, index=1).ass_height_em == 1.0


# ---------------------------------------------------------------------------
# 指定があるとき: 4 か所とも ARG_FONT_DIR の字体
# ---------------------------------------------------------------------------
def test_active_font_from_dir(font_dir):
    font = rf.active_font()
    assert not font.is_default
    assert font.name == FAKE_FAMILY
    assert font.regular == (font_dir / rf.REGULAR_FILE_NAME).resolve()
    assert font.bold == (font_dir / rf.BOLD_FILE_NAME).resolve()
    assert font.fonts_dir == font_dir.resolve()
    assert sl.find_subtitle_font() == font.regular


def test_font_name_can_be_overridden(font_dir, monkeypatch):
    monkeypatch.setenv(rf.ENV_FONT_NAME, "BIZ UDPGothic Custom")
    assert rf.active_font().name == "BIZ UDPGothic Custom"


def test_bold_is_optional(font_dir):
    (font_dir / rf.BOLD_FILE_NAME).unlink()
    assert rf.active_font().bold is None


def test_missing_regular_stops_instead_of_falling_back(tmp_path, monkeypatch):
    monkeypatch.setenv(rf.ENV_FONT_DIR, str(tmp_path / "nowhere"))
    with pytest.raises(rf.RenderFontError):
        rf.active_font()


def test_ass_font_size_comes_from_em(font_dir):
    # 行の高さ 1.0em の字体では、em 70px の字は ASS の Fontsize 70
    assert sl.subtitle_font_size() == 70
    assert sl.uses_line_per_dialogue()


def test_ass_uses_configured_font(tmp_path, font_dir):
    ass = _generate_ass(tmp_path)
    for style in ("Main", "Sub", "Band"):
        assert f"Style: {style},{FAKE_FAMILY},70," in ass
    # 色・縁・余白などフォント以外の欄は既定と同じ
    assert ",&H0055FF55,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,6,2,2,80,80,30,1" in ass
    assert "Meiryo" not in ass


def test_l2_one_dialogue_per_line(tmp_path, font_dir):
    events = _dialogues(_generate_ass(tmp_path))
    assert all("\\N" not in d[9] for d in events if d[3] != "Band")
    # 帯 1 つの後に、その行数ぶんの字幕の Dialogue が同じ時刻で続く
    i = 0
    seen_two_lines = False
    while i < len(events):
        band = events[i]
        assert band[3] == "Band"
        n_lines = int(re.search(r"\\pos\(-?\d+,(\d+)\)", band[9]).group(1))
        n = [k for k in (1, 2) if sl.band_top_y(k) == n_lines][0]
        rows = events[i + 1:i + 1 + n]
        assert len(rows) == n and all(r[3] in ("Main", "Sub") for r in rows)
        assert all((r[1], r[2]) == (band[1], band[2]) for r in rows)
        assert [int(r[7]) for r in rows] == sl.line_margins_v(n)
        seen_two_lines |= n == 2
        i += 1 + n
    assert seen_two_lines


def test_l2_keeps_meiryo_line_positions(font_dir):
    # 各行のベースラインを Meiryo 105 を \N でつないだときと同じ高さに置く
    def baseline_from_bottom(margin_v, descent_em):
        return margin_v + sl.SUBTITLE_EM_PX * descent_em

    meiryo = [
        baseline_from_bottom(sl.MARGIN_V + (1 - k) * sl.FONT_SIZE, rf.MEIRYO_METRICS.descent_em)
        for k in range(2)
    ]
    fake = [baseline_from_bottom(m, FAKE_WIN_DESCENT / FAKE_UPEM) for m in sl.line_margins_v(2)]
    assert all(abs(a - b) <= 0.5 for a, b in zip(meiryo, fake))
    margins = sl.line_margins_v(2)
    assert margins[0] - margins[1] == sl.LINE_PITCH_PX == 105


def test_band_position_does_not_depend_on_font(font_dir, monkeypatch):
    with_font = [sl.band_top_y(n) for n in (1, 2)]
    monkeypatch.delenv(rf.ENV_FONT_DIR)
    assert with_font == [sl.band_top_y(n) for n in (1, 2)]


@pytest.mark.parametrize("module, cls", [
    ("services.video_rendering.ffmpeg_renderer", "FfmpegRenderer"),
    ("services.video_rendering.video_track_renderer", "VideoTrackRenderer"),
])
def test_drawtext_uses_configured_font(font_dir, module, cls):
    # 章タイトルと右上の日付は同じ fontfile を使う
    fontfile = _renderer(module, cls)._get_drawtext_fontfile()
    assert fontfile.endswith(rf.REGULAR_FILE_NAME)
    assert "meiryo" not in fontfile.lower()


def test_date_and_title_drawtext_use_configured_font(font_dir):
    from core.interfaces import ChapterMarker

    renderer = _renderer("services.video_rendering.video_track_renderer", "VideoTrackRenderer")
    assert rf.REGULAR_FILE_NAME in renderer._build_date_filter()
    chapters = [ChapterMarker(
        start_time_sec=0.0, title="家のHDDに当てはめる：1台の確率と前ぶれと備え", section_id="deep_dive_1"
    )]
    overlay = renderer._build_topic_overlay_filter(chapters, 30.0)
    assert overlay.count(rf.REGULAR_FILE_NAME) == 2  # 2 行に分かれ、どちらの行も同じ字体


def test_subtitle_filter_passes_fonts_dir(tmp_path, font_dir):
    renderer = _renderer("services.video_rendering.video_track_renderer", "VideoTrackRenderer")
    vf = renderer._build_subtitle_filter(tmp_path / "subtitles.ass")
    assert vf.startswith("subtitles='")
    expected = str(font_dir.resolve()).replace("\\", "/").replace(":", "\\:")
    assert vf.endswith(f":fontsdir='{expected}'")


def test_ffmpeg_renderer_subtitle_filter_passes_fonts_dir():
    # FfmpegRenderer は render の中で組み立てるので、組み立ての形をソースで確かめる
    src = (ROOT / "services/video_rendering/ffmpeg_renderer.py").read_text(encoding="utf-8")
    assert ":fontsdir='" in src
    assert "render_fonts.active_font().fonts_dir" in src


def test_thumbnail_uses_configured_font(font_dir):
    from services.media_processing.thumbnail_generator import ThumbnailGenerator

    font = rf.active_font()
    assert ThumbnailGenerator()._find_japanese_fonts() == [font.regular, font.bold]


def test_sample_script_writes_ass_with_configured_font(tmp_path, font_dir):
    # 見比べ用の静止画の書き出し (scripts/render_font_samples.py) は本番と同じ組み立てを使う
    import importlib.util

    spec = importlib.util.spec_from_file_location("render_font_samples", ROOT / "scripts/render_font_samples.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ass = module._write_ass(tmp_path).read_text(encoding="utf-8")
    assert f"Style: Main,{FAKE_FAMILY},70," in ass
    assert len(module._chapters()) == len(module.SAMPLES)


def test_budoux_is_pinned():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^budoux==0\.8\.0\b", text, re.M)


# ---------------------------------------------------------------------------
# 本物の BIZ UDPGothic があるときだけ
# ---------------------------------------------------------------------------
def _real_font_dir():
    for candidate in (os.environ.get("ARG_TEST_FONT_DIR"), str(Path.home() / "radio-fonts")):
        if candidate and (Path(candidate) / rf.REGULAR_FILE_NAME).is_file():
            return Path(candidate)
    return None


REAL_FONT_DIR = _real_font_dir()
needs_real_font = pytest.mark.skipif(REAL_FONT_DIR is None, reason="BIZ UDPGothic のある環境だけ")


@needs_real_font
def test_real_font_metrics_and_em(monkeypatch):
    monkeypatch.setenv(rf.ENV_FONT_DIR, str(REAL_FONT_DIR))
    monkeypatch.delenv(rf.ENV_FONT_NAME, raising=False)
    font = rf.active_font()
    assert font.name == "BIZ UDPGothic"
    assert font.bold is not None
    # 調べ (survey §6) では Meiryo 105 と同じ字の大きさは 70 相当
    assert 66 <= sl.subtitle_font_size() <= 74
    # 全角の字は em 70px で 70px 幅 (字幕の幅の判定が画面と同じ大きさで測れている)
    measure = sl.make_font_measurer(font.regular)
    assert abs(measure("漢") - 70) <= 1


@needs_real_font
def test_real_font_lines_fit_safe_area(tmp_path, monkeypatch):
    monkeypatch.setenv(rf.ENV_FONT_DIR, str(REAL_FONT_DIR))
    measure = sl.make_font_measurer(rf.active_font().regular)
    subs = [d for d in _dialogues(_generate_ass(tmp_path)) if d[3] != "Band"]
    assert subs
    for d in subs:
        assert measure(d[9]) <= sl.MAX_TEXT_WIDTH_PX
