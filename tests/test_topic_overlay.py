"""章タイトルのオーバーレイ (services/video_rendering/topic_overlay.py) のテスト

2026-10-23: 章タイトルが 20 字で「...」に切られ、CO2 回では 6 章すべて全文が出ていなかった。
字体も字幕 (Meiryo) と違っていた (MS ゴシック)。切らずに折り返し、字幕と同じ Meiryo で出す。
"""
import importlib
import re

import pytest

from core.interfaces import ChapterMarker
from services.video_rendering import topic_overlay as to

TITLES = [
    "はじめに",
    "CO2はなぜ温暖化の原因になるのか",  # 17 字
    "排出を抑えるには何が必要で、どこまで減らせるのか",  # 24 字
    "大気中の二酸化炭素の濃度と産業革命から現在までの推移を数字で読み解く回",  # 35 字
]


def _escape(text: str) -> str:
    return text


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


@pytest.mark.parametrize("title", TITLES)
def test_title_is_not_truncated(title):
    size, lines = to.layout_title(title)
    assert _squash("".join(lines)) == _squash(title)
    assert "..." not in "".join(lines)
    assert 1 <= len(lines) <= to.TITLE_MAX_LINES
    assert size in to.TITLE_FONT_SIZES


@pytest.mark.parametrize("title", TITLES)
def test_title_lines_fit_frame(title):
    size, lines = to.layout_title(title)
    measure = to._title_measurer(size)
    assert all(measure(line) <= to.TITLE_MAX_WIDTH_PX for line in lines)


def test_short_title_keeps_largest_size_on_one_line():
    size, lines = to.layout_title("はじめに")
    assert size == to.TITLE_FONT_SIZES[0]
    assert lines == ["はじめに"]


def test_very_long_title_shrinks_and_keeps_every_character():
    title = "とても長い章タイトルが来ても文字を一つも落とさずに全部出す" * 4
    size, lines = to.layout_title(title)
    assert size == to.TITLE_FONT_SIZES[-1]
    assert _squash("".join(lines)) == _squash(title)


def test_title_shrinks_before_using_third_line():
    # 最大の字で 3 行になる長さなら、字を小さくして 2 行に収める
    measure = lambda size: (lambda s: size * len(s))
    title = "あ" * 30  # 80px なら 1 行 21 字 → 2 行、 4 行目まで要らない
    size, lines = to.layout_title(title, measure, 1700)
    assert (size, len(lines)) == (80, 2)
    title = "あ" * 50  # 80px: 3 行 / 72px: 1 行 23 字 → 3 行 / 64px: 26 字 → 2 行
    size, lines = to.layout_title(title, measure, 1700)
    assert (size, len(lines)) == (64, 2)


def test_overlay_filter_has_one_drawtext_per_line():
    chapters = [
        ChapterMarker(start_time_sec=0.0, title=TITLES[0], section_id="intro"),
        ChapterMarker(start_time_sec=30.0, title=TITLES[3], section_id="deep_dive_1"),
    ]
    overlay = to.build_topic_overlay_filter(chapters, 90.0, "C\\:/Windows/Fonts/meiryo.ttc", _escape)
    drawtexts = overlay.split(",drawtext=")
    n_lines = sum(len(to.layout_title(c.title)[1]) for c in chapters)
    assert len(drawtexts) == n_lines
    assert "..." not in overlay
    assert "meiryo.ttc" in overlay
    assert "between(t,0.000,30.000)" in overlay
    assert "between(t,30.000,90.000)" in overlay


def test_overlay_lines_are_stacked_without_overlap():
    drawtexts = to.build_title_drawtexts(TITLES[3], 0.0, 10.0, "font", _escape)
    assert len(drawtexts) == 2
    size = int(re.search(r":fontsize=(\d+):", drawtexts[0]).group(1))
    ys = [int(re.search(r":y=(\d+):", d).group(1)) for d in drawtexts]
    # 行の高さ (em の約 1.5 倍) と上下の箱の枠を足した分だけ下げる
    assert ys[1] - ys[0] >= size * 1.5 + 2 * to.TITLE_BOX_BORDER - 1


def test_empty_chapters_give_empty_filter():
    assert to.build_topic_overlay_filter([], 10.0, "font", _escape) == ""
    assert to.build_topic_overlay_filter(None, 10.0, "font", _escape) == ""


@pytest.mark.parametrize("module, cls", [
    ("services.video_rendering.ffmpeg_renderer", "FfmpegRenderer"),
    ("services.video_rendering.video_track_renderer", "VideoTrackRenderer"),
])
def test_renderers_prefer_meiryo_like_subtitles(module, cls):
    # Windows では Meiryo が見つかり、フォントの無い環境では先頭候補 (Meiryo) が返る
    renderer_cls = getattr(importlib.import_module(module), cls)
    renderer = renderer_cls.__new__(renderer_cls)
    assert renderer._get_drawtext_fontfile().endswith("meiryo.ttc")
