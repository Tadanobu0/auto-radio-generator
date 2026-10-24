"""章タイトルのオーバーレイ (画面上部の帯) を drawtext で組み立てる。

2026-10-23: 14〜16 字の章タイトルが「...」で切られ、6 章すべて全文が出ていなかった
(radio_director-012 のコマ評価)。切らずに出すため、字幕と同じ Meiryo の実測幅で
折り返し (最大 TITLE_MAX_LINES 行)、それでも収まらなければ字を小さくする。
1 行ごとに drawtext を 1 つ置き、行ごとに中央揃えにする。

2026-10-23 (radio_director-013 のコマ評価):
  - 「家のHDDに当てはめる：1台の確率と前ぶれと備え」が「：」の前で折れ、2 行目の頭に
    「：」が来た。題に「：」「: 」があれば、そこで 1 行目 (主題) と 2 行目 (副題) に分ける
  - drawtext は既定で text の「%」を展開記法として読み、「%{」以外が続くと文字を描かない。
    「40%の失敗」のような題が丸ごと出なくなるので、題の drawtext は expansion=none にする
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Callable, Optional

from core.interfaces import ChapterMarker
from services.audio_synthesis import subtitle_layout as sl

FRAME_WIDTH = 1920

# 右上の日付 (video_track_renderer の drawtext)。Meiryo 28px の「2026/10/24」は 162px (Windows で実測)
DATE_FONT_SIZE = 28
DATE_MARGIN_X = 20
DATE_MARGIN_Y = 20
DATE_MAX_WIDTH_PX = 170
TITLE_DATE_GAP = 20

TITLE_FONT_SIZES = (80, 72, 64, 56)
TITLE_MAX_LINES = 2
# 2026-10-24 (010): 横に長い題の帯が右上の日付と重なった (帯は y=40 から、日付は y=20〜60 ほど)。
# 帯は中央揃えなので、左右とも日付の幅 + 間隔のぶん内側に収める (100 → 210、最大幅 1700 → 1480px)
TITLE_MARGIN_X = DATE_MARGIN_X + DATE_MAX_WIDTH_PX + TITLE_DATE_GAP
TITLE_TOP_Y = 50
TITLE_BOX_BORDER = 10
TITLE_MAX_WIDTH_PX = FRAME_WIDTH - 2 * (TITLE_MARGIN_X + TITLE_BOX_BORDER)

# 主題と副題の区切り。全角の「：」か、空白が続く半角の「:」(「10:30」は区切りにしない)。
# 空白が続く「？」「！」も区切りにする (「電波はどこで弱まる？ 距離・壁・水と金属」)
_SUBTITLE_COLON = re.compile(r"：\s*|:\s+|[？！?!]\s+")
_COLONS = "：:"


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

    最大の字で 1 行に収まればそのまま。収まらず、題に「：」「: 」「？ 」があれば、主題と副題が
    それぞれ 1 行に収まる最大の字で、そこで 2 行に分ける (「：」は 1 行目の末尾に残す)。
    どちらでもなければ大きい字から順に実測幅で折り返し、TITLE_MAX_LINES 行に収まる最初の
    大きさを使う。最小の大きさでも収まらないときは、行数を増やして全文を出す。
    """
    title = " ".join(title.split())
    if not title:
        return TITLE_FONT_SIZES[0], []
    if measurer(TITLE_FONT_SIZES[0])(title) <= max_width:
        return TITLE_FONT_SIZES[0], [title]
    parts = _split_at_colon(title)
    if parts is not None:
        for size in TITLE_FONT_SIZES:
            measure = measurer(size)
            if all(measure(part) <= max_width for part in parts):
                return size, list(parts)
    for size in TITLE_FONT_SIZES:
        measure = measurer(size)
        lines = _keep_colon_at_line_end(sl.wrap_lines(title, measure, max_width), measure, max_width)
        if len(lines) <= TITLE_MAX_LINES:
            return size, lines
    return size, lines


def _split_at_colon(title: str) -> Optional[tuple[str, str]]:
    """最初の「：」「: 」で (主題, 副題) に分ける。どちらかが空なら None。"""
    m = _SUBTITLE_COLON.search(title)
    if m is None:
        return None
    head = title[:m.start()].rstrip()
    tail = title[m.end():].strip()
    if not head or not tail:
        return None
    return head + title[m.start()], tail


def _keep_colon_at_line_end(lines: list[str], measure: sl.Measure, max_width: float) -> list[str]:
    """行頭に来た「：」「:」を前の行の末尾に移す (移すと幅を超えるならそのまま)。"""
    lines = list(lines)
    for k in range(1, len(lines)):
        if lines[k][:1] in _COLONS:
            moved = lines[k - 1] + lines[k][0]
            rest = lines[k][1:].lstrip()
            if rest and measure(moved) <= max_width:
                lines[k - 1], lines[k] = moved, rest
    return lines


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
        f"drawtext=text='{escape(line)}':expansion=none:"
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
