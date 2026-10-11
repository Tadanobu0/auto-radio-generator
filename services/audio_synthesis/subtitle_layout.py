"""字幕の表示層: 出典タグの除去、描画幅での折り返し、キュー分割、時間配分。

verified_script.json と phrase_data (1ターン=1要素) は変更しない。ここは
「1ターンのテキストと発話区間」を受け取り、画面に出すキュー列を返すだけの純関数群。

不変条件:
  - 全行の描画幅が MAX_TEXT_WIDTH_PX 以下 (文字数ではなく実フォントの幅で判定)
  - 1キューは原則 PREFERRED_LINES 行、上限 MAX_LINES 行
  - 分割したキューの表示時間は、発話区間の中で文字数比で配る
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional, Sequence

logger = logging.getLogger(__name__)

# --- ASS スタイルと同じ値 (voicevox_client._generate_ass のヘッダーと揃える) ---
PLAY_RES_X = 1920
FONT_NAME = "Meiryo"
FONT_SIZE = 105
MARGIN_L = 80
MARGIN_R = 80
OUTLINE = 6
SHADOW = 2

SAFE_WIDTH_PX = PLAY_RES_X - MARGIN_L - MARGIN_R
# 縁取りと影も左右マージンの内側に収める
MAX_TEXT_WIDTH_PX = SAFE_WIDTH_PX - 2 * (OUTLINE + SHADOW)

PREFERRED_LINES = 2
MAX_LINES = 3
# これより短いキューは隣と結合する (結合して MAX_LINES を超えるなら短いまま出す)
MIN_CUE_MS = 1200

FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/meiryo.ttc"),
)

Measure = Callable[[str], float]

_CITATION_TAG = re.compile(r"\[src=\d+(?:\s*,\s*\d+)*\]")
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t\u3000]+(?=[、。，．！？!?,.])")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_SENTENCE = re.compile(r"[^。！？!?]*[。！？!?]+[」』）)】\]]*|[^。！？!?]+$")
_CLAUSE = re.compile(r"[^、，,]*[、，,]+|[^、，,]+$")
# 行頭に置かない文字 (文字単位で割るときだけ使う。BudouX の文節境界では起きない)
_NO_LINE_START = "、。，．！？!?,.」』）)】]ー〜…ゃゅょっャュョッ"


# ---------------------------------------------------------------------------
# 出典タグの除去
# ---------------------------------------------------------------------------
def strip_for_display(text: str) -> str:
    """[src=N] を除去し、除去で崩れた空白・句読点を整えて返す。

    - 英数字どうしの間にあり、もともと空白で区切られていたタグは空白1つに、それ以外は詰める
    - 句読点の前に残った空白を消す
    - タグを挟んで同じ句読点が並んだら1つにする
    """
    out: list[str] = []
    pos = 0
    for m in _CITATION_TAG.finditer(text):
        out.append(text[pos:m.start()])
        pos = m.end()
        before = "".join(out)
        head = before.rstrip(" \t\u3000")
        tail = text[pos:].lstrip(" \t\u3000")
        had_space = len(head) != len(before) or len(tail) != len(text) - pos
        prev_ch = head[-1:] if head else ""
        next_ch = tail[:1]
        out = [head]
        if had_space and _is_ascii_word(prev_ch) and _is_ascii_word(next_ch):
            out.append(" ")
        elif prev_ch and prev_ch == next_ch and prev_ch in "、。":
            tail = tail[1:]
        pos = len(text) - len(tail)
    out.append(text[pos:])
    cleaned = "".join(out)
    cleaned = _SPACE_BEFORE_PUNCT.sub("", cleaned)
    cleaned = _MULTI_SPACE.sub(" ", cleaned)
    return cleaned.strip()


def _is_ascii_word(ch: str) -> bool:
    return bool(ch) and ch.isascii() and not ch.isspace()


# ---------------------------------------------------------------------------
# 描画幅の計測
# ---------------------------------------------------------------------------
def find_subtitle_font() -> Optional[Path]:
    """字幕フォント (Meiryo) の実ファイルを返す。無ければ None。"""
    for candidate in FONT_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def make_font_measurer(font_path: Path, font_size: int = FONT_SIZE) -> Measure:
    """実フォントで描画幅 (px) を返す関数を作る。

    libass は ASS の Fontsize を「アセンダ+ディセンダの高さ」として扱うので、
    em サイズは font_size * em / (ascender + descender) になる (Meiryo 105 → 70px)。
    """
    from PIL import ImageFont

    probe_size = 4096
    probe = ImageFont.truetype(str(font_path), probe_size, layout_engine=ImageFont.Layout.BASIC)
    ascent, descent = probe.getmetrics()
    em_px = font_size * probe_size / (ascent + descent)
    font = ImageFont.truetype(str(font_path), em_px, layout_engine=ImageFont.Layout.BASIC)

    @lru_cache(maxsize=65536)
    def measure(text: str) -> float:
        return float(font.getlength(text))

    return measure


def estimate_width(text: str, font_size: int = FONT_SIZE) -> float:
    """フォントが無い環境向けの概算幅。実フォントより広めに見積もる。"""
    em_px = font_size * 2 / 3
    width = 0.0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ("W", "F", "A"):
            width += em_px
        elif ch.isupper() or ch.isdigit():
            width += em_px * 0.9
        else:
            width += em_px * 0.7
    return width


@lru_cache(maxsize=1)
def default_measurer() -> Measure:
    """本番用の計測関数。Meiryo があれば実測、無ければ概算に落とす。"""
    font_path = find_subtitle_font()
    if font_path is not None:
        try:
            return make_font_measurer(font_path)
        except Exception as e:  # フォント破損や Pillow の不整合で字幕生成を止めない
            logger.warning(f"字幕フォントを読めないため概算幅で折り返す: {e}")
            return estimate_width
    logger.warning("字幕フォント (Meiryo) が見つからないため概算幅で折り返す")
    return estimate_width


# ---------------------------------------------------------------------------
# 折り返し
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _budoux_parser():
    from budoux import load_default_japanese_parser

    return load_default_japanese_parser()


def _break_pieces(text: str, measure: Measure, max_width: float) -> list[str]:
    """改行してよい位置で区切った断片の列を返す (連結すると text に戻る)。

    文節 (BudouX) → 空白の直後 → それでも幅を超える断片は文字単位。
    """
    pieces: list[str] = []
    for chunk in _budoux_parser().parse(text):
        for part in re.findall(r"\S*\s*", chunk):
            if not part:
                continue
            while measure(part.strip()) > max_width:
                cut = len(part) - 1
                while cut > 1 and measure(part[:cut].strip()) > max_width:
                    cut -= 1
                while cut > 1 and part[cut] in _NO_LINE_START:
                    cut -= 1
                pieces.append(part[:cut])
                part = part[cut:]
            pieces.append(part)
    return pieces


def _greedy_lines(pieces: Sequence[str], measure: Measure, max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for piece in pieces:
        if not current.strip() or measure((current + piece).strip()) <= max_width:
            current += piece
        else:
            lines.append(current.strip())
            current = piece
    if current.strip():
        lines.append(current.strip())
    return lines


def _balanced_lines(pieces: Sequence[str], n_lines: int, measure: Measure, max_width: float) -> Optional[list[str]]:
    """n_lines 行のまま、行の長さがなるべく揃う切り方を返す (末尾に1〜2文字だけ残るのを避ける)。

    英単語の間 (空白) での改行は、文節の切れ目での改行より後回しにする。
    """
    count = len(pieces)
    # 断片 i の直後で改行すると英単語の間で切ることになるか
    word_gap = [
        p[-1:].isspace() and p.strip()[-1:].isascii() and pieces[i + 1][:1].isascii()
        for i, p in enumerate(pieces[:-1])
    ]

    def width(i: int, j: int) -> float:
        return measure("".join(pieces[i:j]).strip())

    inf = (float("inf"), float("inf"))
    # best[k][j]: 先頭 j 断片を k 行に収めたときの (英単語間の改行数, 最大行幅) の最小値
    best = [[inf] * (count + 1) for _ in range(n_lines + 1)]
    back = [[0] * (count + 1) for _ in range(n_lines + 1)]
    best[0][0] = (0, 0.0)
    for k in range(1, n_lines + 1):
        for j in range(k, count + 1):
            for i in range(k - 1, j):
                if best[k - 1][i] == inf:
                    continue
                w = width(i, j)
                if w > max_width:
                    continue
                gaps = best[k - 1][i][0] + (1 if 0 < i and word_gap[i - 1] else 0)
                cost = (gaps, max(best[k - 1][i][1], w))
                if cost < best[k][j]:
                    best[k][j] = cost
                    back[k][j] = i
    if best[n_lines][count] == inf:
        return None
    lines: list[str] = []
    j = count
    for k in range(n_lines, 0, -1):
        i = back[k][j]
        lines.append("".join(pieces[i:j]).strip())
        j = i
    lines.reverse()
    return lines if all(lines) else None


def wrap_lines(text: str, measure: Optional[Measure] = None, max_width: float = MAX_TEXT_WIDTH_PX) -> list[str]:
    """text を描画幅 max_width 以下の行に折り返す。行数は最小、行の長さはなるべく揃える。"""
    measure = measure or default_measurer()
    text = text.strip()
    if not text:
        return []
    pieces = _break_pieces(text, measure, max_width)
    lines = _greedy_lines(pieces, measure, max_width)
    if len(lines) > 1:
        lines = _balanced_lines(pieces, len(lines), measure, max_width) or lines
    return lines


def _line_count(text: str, measure: Measure, max_width: float) -> int:
    text = text.strip()
    if not text:
        return 0
    return len(_greedy_lines(_break_pieces(text, measure, max_width), measure, max_width))


# ---------------------------------------------------------------------------
# キュー分割
# ---------------------------------------------------------------------------
def _spans(text: str, pattern: re.Pattern, offset: int = 0) -> list[tuple[int, int]]:
    return [
        (offset + m.start(), offset + m.end())
        for m in pattern.finditer(text)
        if m.group(0).strip()
    ]


def split_cues(text: str, measure: Optional[Measure] = None, max_width: float = MAX_TEXT_WIDTH_PX) -> list[str]:
    """表示用テキストを、MAX_LINES 行以内のキュー列に分ける。

    文末で区切り、PREFERRED_LINES 行に収まる範囲で隣の文とまとめる。1文が MAX_LINES 行を
    超えるときだけ読点で、それでも超えるなら行の切れ目で分ける。フォントは縮小しない。
    """
    measure = measure or default_measurer()
    text = text.strip()
    if not text:
        return []

    def lines_of(start: int, end: int) -> int:
        return _line_count(text[start:end], measure, max_width)

    def pack(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
        packed: list[tuple[int, int]] = []
        for start, end in spans:
            if packed and lines_of(packed[-1][0], end) <= PREFERRED_LINES:
                packed[-1] = (packed[-1][0], end)
            else:
                packed.append((start, end))
        return packed

    atoms: list[tuple[int, int]] = []
    for s_start, s_end in _spans(text, _SENTENCE):
        if lines_of(s_start, s_end) <= MAX_LINES:
            atoms.append((s_start, s_end))
            continue
        parts: list[tuple[int, int]] = []
        for c_start, c_end in _spans(text[s_start:s_end], _CLAUSE, s_start):
            if lines_of(c_start, c_end) <= MAX_LINES:
                parts.append((c_start, c_end))
                continue
            # 読点でも割れない長さ: 行の切れ目で PREFERRED_LINES 行ずつに分ける
            pieces = _break_pieces(text[c_start:c_end], measure, max_width)
            pos = c_start
            group_start = c_start
            current = ""
            n_in_group = 1
            for piece in pieces:
                if current.strip() and measure((current + piece).strip()) > max_width:
                    if n_in_group == PREFERRED_LINES:
                        parts.append((group_start, pos))
                        group_start = pos
                        n_in_group = 1
                    else:
                        n_in_group += 1
                    current = piece
                else:
                    current += piece
                pos += len(piece)
            parts.append((group_start, c_end))
        atoms.extend(pack(parts))

    return [text[start:end].strip() for start, end in pack(atoms) if text[start:end].strip()]


# ---------------------------------------------------------------------------
# 時間配分
# ---------------------------------------------------------------------------
def _weight(cue_text: str) -> int:
    return max(1, sum(1 for ch in cue_text if not ch.isspace()))


def allocate_times(cue_texts: Sequence[str], start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """発話区間 [start_ms, end_ms] を文字数比でキューに配る。

    境界は ASS の分解能 (10ms) に丸め、前のキューの終わり=次のキューの始まりにする。
    """
    if not cue_texts:
        return []
    weights = [_weight(t) for t in cue_texts]
    total = sum(weights)
    duration = max(0, end_ms - start_ms)
    bounds = [start_ms]
    acc = 0
    for w in weights[:-1]:
        acc += w
        bounds.append(start_ms + int(round(duration * acc / total / 10.0)) * 10)
    bounds.append(end_ms)
    for i in range(1, len(bounds)):
        bounds[i] = max(bounds[i], bounds[i - 1])
    return [(bounds[i], bounds[i + 1]) for i in range(len(cue_texts))]


def _merge_short_cues(
    cue_texts: list[str], start_ms: int, end_ms: int, measure: Measure, max_width: float
) -> list[str]:
    """MIN_CUE_MS 未満のキューを隣と結合する。結合後が MAX_LINES を超えるなら結合しない。"""
    cue_texts = list(cue_texts)
    stuck: set[str] = set()
    while len(cue_texts) > 1:
        times = allocate_times(cue_texts, start_ms, end_ms)
        short = [
            (e - s, i) for i, (s, e) in enumerate(times)
            if e - s < MIN_CUE_MS and cue_texts[i] not in stuck
        ]
        if not short:
            break
        _, i = min(short)
        options = []
        for j in (i - 1, i + 1):
            if 0 <= j < len(cue_texts):
                lo, hi = min(i, j), max(i, j)
                merged = _join(cue_texts[lo], cue_texts[hi])
                n = _line_count(merged, measure, max_width)
                if n <= MAX_LINES:
                    options.append((n, _weight(cue_texts[j]), lo, merged))
        if not options:
            stuck.add(cue_texts[i])
            continue
        _, _, lo, merged = min(options)
        cue_texts[lo:lo + 2] = [merged]
    return cue_texts


def _join(left: str, right: str) -> str:
    if left[-1:].isascii() and right[:1].isascii():
        return f"{left} {right}"
    return left + right


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SubtitleCue:
    start_ms: int
    end_ms: int
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "".join(self.lines)


def layout_turn(
    text: str,
    start_ms: int,
    end_ms: int,
    measure: Optional[Measure] = None,
    max_width: float = MAX_TEXT_WIDTH_PX,
) -> list[SubtitleCue]:
    """1ターンのテキストと発話区間から、画面に出すキュー列を作る。"""
    measure = measure or default_measurer()
    display = strip_for_display(text)
    cue_texts = split_cues(display, measure, max_width)
    cue_texts = _merge_short_cues(cue_texts, start_ms, end_ms, measure, max_width)
    times = allocate_times(cue_texts, start_ms, end_ms)
    return [
        SubtitleCue(start_ms=s, end_ms=e, lines=tuple(wrap_lines(t, measure, max_width)))
        for t, (s, e) in zip(cue_texts, times)
    ]
