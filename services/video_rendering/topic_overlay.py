"""章タイトルのオーバーレイ (画面上部の帯) を drawtext で組み立てる。

2026-10-23: 14〜16 字の章タイトルが「...」で切られ、6 章すべて全文が出ていなかった
(radio_director-012 のコマ評価)。切らずに出すため、字幕と同じ Meiryo の実測幅で
折り返し (最大 TITLE_MAX_LINES 行)、それでも収まらなければ字を小さくする。
1 行ごとに drawtext を 1 つ置き、行ごとに中央揃えにする。
"""
from __future__ import annotations

from functools import lru_cache
from typing import Callable

from core.interfaces import ChapterMarker
from services.audio_synthesis import subtitle_layout as sl

FRAME_WIDTH = 1920
TITLE_FONT_SIZES = (80, 72, 64, 56)
TITLE_MAX_LINES = 2
TITLE_MARGIN_X = 100
TITLE_TOP_Y = 50
TITLE_BOX_BORDER = 10
TITLE_MAX_WIDTH_PX = FRAME_WIDTH - 2 * (TITLE_MARGIN_X + TITLE_BOX_BORDER)


@lru_cache(maxsize=None)
def _title_measurer(font_size: int) -> sl.Measure:
    """drawtext の fontsize (= em の px) での描画幅を返す関数。Meiryo が無ければ概算。"""
    font_path = sl.find_subtitle_font()
    if font_path is not None:
        try:
            from PIL import ImageFont

            font = ImageFont.truetype(str(font_path), font_size, layout_engine=ImageFont.Layout.BASIC)
            return lambda text: float(font.getlength(text))
        except Exception:
            pass
    # estimate_width は ASS の Fontsize (em の 1.5 倍) を受け取る
    return lambda text: sl.estimate_width(text, font_size * 1.5)


def layout_title(
    title: str,
    measurer: Callable[[int], sl.Measure] = _title_measurer,
    max_width: float = TITLE_MAX_WIDTH_PX,
) -> tuple[int, list[str]]:
    """章タイトルを (fontsize, 行の列) にする。文字は落とさない。

    大きい字から順に試し、TITLE_MAX_LINES 行に収まる最初の大きさを使う。最小の大きさでも
    収まらないときは、行数を増やして全文を出す。
    """
    title = " ".join(title.split())
    if not title:
        return TITLE_FONT_SIZES[0], []
    for size in TITLE_FONT_SIZES:
        lines = sl.wrap_lines(title, measurer(size), max_width)
        if len(lines) <= TITLE_MAX_LINES:
            return size, lines
    return size, lines


def build_title_drawtexts(
    title: str,
    start_sec: float,
    end_sec: float,
    fontfile: str,
    escape: Callable[[str], str],
) -> list[str]:
    """1 章ぶんの drawtext フィルタ (1 行に 1 つ) を返す。"""
    size, lines = layout_title(title)
    # Meiryo の 1 行の高さは em の約 1.5 倍。行ごとの箱が重ならないよう枠の分も空ける
    line_step = int(round(size * 1.5)) + 2 * TITLE_BOX_BORDER
    return [
        f"drawtext=text='{escape(line)}':"
        f"fontfile='{fontfile}':"
        f"fontsize={size}:fontcolor=white:"
        f"x=(w-text_w)/2:y={TITLE_TOP_Y + n * line_step}:"
        f"box=1:boxcolor=black@0.5:boxborderw={TITLE_BOX_BORDER}:"
        f"enable='between(t,{start_sec:.3f},{end_sec:.3f})'"
        for n, line in enumerate(lines)
    ]


def build_topic_overlay_filter(
    chapters: list[ChapterMarker] | None,
    total_duration_sec: float,
    fontfile: str,
    escape: Callable[[str], str],
) -> str:
    """全章ぶんの drawtext をつないだフィルタ文字列。章が無ければ空文字。"""
    if not chapters:
        return ""
    ordered = sorted(chapters, key=lambda c: c.start_time_sec)
    filters: list[str] = []
    for i, chapter in enumerate(ordered):
        start_sec = max(0.0, float(chapter.start_time_sec))
        if i + 1 < len(ordered):
            end_sec = float(ordered[i + 1].start_time_sec)
        else:
            end_sec = float(total_duration_sec)
        if end_sec <= start_sec:
            continue
        filters.extend(build_title_drawtexts(chapter.title, start_sec, end_sec, fontfile, escape))
    return ",".join(filters)
