"""書き出しに使うフォント (字幕・章タイトル・右上の日付・サムネイル) の受け口。

2026-11-01 (017): 書き出しを Mac に移すため、4 か所で固定していた Meiryo
(C:/Windows/Fonts/meiryo.ttc と ASS の「Meiryo,105」) を設定で差し替えられるようにした。

- 環境変数 ARG_FONT_DIR が無ければ、今までどおり Meiryo を使う (既存の本番を変えない)
- ARG_FONT_DIR があれば、そのフォルダの BIZUDPGothic-Regular.ttf / BIZUDPGothic-Bold.ttf を使う。
  ASS のフォント名はファイルの name テーブルの family 名 (ARG_FONT_NAME で上書きできる)。
  Regular が無ければ RenderFontError で止める (指定したのに黙って Meiryo に落とさない)

字の大きさは「em の px」を正にする。drawtext の fontsize と Pillow の size は em の px だが、
libass は ASS の Fontsize を OS/2 の usWinAscent + usWinDescent の高さとして扱う。
そのため ASS の Fontsize は em の px × (winAscent + winDescent) / unitsPerEm で出す
(Meiryo は 2171 + 901 = 3072 = 1.5 em なので em 70px → 105。今と同じ)。
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

ENV_FONT_DIR = "ARG_FONT_DIR"
ENV_FONT_NAME = "ARG_FONT_NAME"
REGULAR_FILE_NAME = "BIZUDPGothic-Regular.ttf"
BOLD_FILE_NAME = "BIZUDPGothic-Bold.ttf"


class RenderFontError(FileNotFoundError):
    """ARG_FONT_DIR を指定したのにフォントが使えないとき。"""


@dataclass(frozen=True)
class FontMetrics:
    """libass が字の大きさを決めるのに使う寸法 (フォント単位)。"""

    units_per_em: int
    win_ascent: int
    win_descent: int
    family_name: str = ""

    @property
    def ass_height_em(self) -> float:
        """ASS の Fontsize 1 に対する em の比の逆数 (= Fontsize / em の px)。"""
        return (self.win_ascent + self.win_descent) / self.units_per_em

    @property
    def descent_em(self) -> float:
        """libass の行の下端からベースラインまで (em 比)。"""
        return self.win_descent / self.units_per_em


# Meiryo (meiryo.ttc の 1 番目) の寸法。Windows の実機で測った値で、Meiryo が無い環境
# (Mac・試験) でも ASS のヘッダーが「Meiryo,105」のままになるよう固定で持つ
MEIRYO_METRICS = FontMetrics(units_per_em=2048, win_ascent=2171, win_descent=901, family_name="Meiryo")


@dataclass(frozen=True)
class RenderFont:
    """書き出しに使うフォント一式。"""

    name: str  # ASS の Fontname
    regular: Path
    bold: Optional[Path]
    fonts_dir: Optional[Path]  # libass に渡すフォルダ (subtitles の fontsdir)。Meiryo は None (システムのフォント)
    metrics: FontMetrics
    is_default: bool  # True = 今までの Meiryo

    def ass_font_size(self, em_px: float) -> int:
        """em の px から ASS の Fontsize を出す。"""
        return int(round(em_px * self.metrics.ass_height_em))


MEIRYO = RenderFont(
    name="Meiryo",
    regular=Path("C:/Windows/Fonts/meiryo.ttc"),
    bold=Path("C:/Windows/Fonts/meiryob.ttc"),
    fonts_dir=None,
    metrics=MEIRYO_METRICS,
    is_default=True,
)


def active_font() -> RenderFont:
    """今の設定 (環境変数) で使うフォント。呼ぶたびに環境変数を読む。"""
    font_dir = os.environ.get(ENV_FONT_DIR, "").strip()
    if not font_dir:
        return MEIRYO
    return _font_from_dir(font_dir, os.environ.get(ENV_FONT_NAME, "").strip())


@lru_cache(maxsize=8)
def _font_from_dir(font_dir: str, name_override: str) -> RenderFont:
    directory = Path(font_dir).expanduser()
    regular = directory / REGULAR_FILE_NAME
    if not regular.is_file():
        raise RenderFontError(f"{ENV_FONT_DIR}={font_dir} に {REGULAR_FILE_NAME} がありません")
    bold = directory / BOLD_FILE_NAME
    try:
        metrics = read_font_metrics(regular)
    except (OSError, ValueError, struct.error) as e:
        raise RenderFontError(f"{regular} の寸法を読めません: {e}") from e
    name = name_override or metrics.family_name
    if not name:
        raise RenderFontError(f"{regular} の family 名を読めません ({ENV_FONT_NAME} で指定してください)")
    return RenderFont(
        name=name,
        regular=regular.resolve(),
        bold=bold.resolve() if bold.is_file() else None,
        fonts_dir=directory.resolve(),
        metrics=metrics,
        is_default=False,
    )


# ---------------------------------------------------------------------------
# フォントファイルの寸法 (OS/2・head・name テーブル) を読む。標準ライブラリだけで読む
# ---------------------------------------------------------------------------
def read_font_metrics(path: Path, index: int = 0) -> FontMetrics:
    """TrueType / OpenType / TTC から unitsPerEm・usWinAscent・usWinDescent・family 名を読む。"""
    data = Path(path).read_bytes()
    base = 0
    if data[:4] == b"ttcf":
        count = struct.unpack_from(">I", data, 8)[0]
        if not 0 <= index < count:
            raise ValueError(f"font index {index} out of range ({count})")
        base = struct.unpack_from(">I", data, 12 + 4 * index)[0]
    num_tables = struct.unpack_from(">H", data, base + 4)[0]
    tables: dict[bytes, int] = {}
    for i in range(num_tables):
        tag, _checksum, offset, _length = struct.unpack_from(">4sIII", data, base + 12 + 16 * i)
        tables[tag] = offset
    if b"head" not in tables or b"OS/2" not in tables:
        raise ValueError("head or OS/2 table missing")
    units_per_em = struct.unpack_from(">H", data, tables[b"head"] + 18)[0]
    win_ascent, win_descent = struct.unpack_from(">HH", data, tables[b"OS/2"] + 74)
    if units_per_em <= 0 or win_ascent + win_descent <= 0:
        raise ValueError("invalid font metrics")
    family = _read_family_name(data, tables.get(b"name"))
    return FontMetrics(units_per_em, win_ascent, win_descent, family)


def _read_family_name(data: bytes, offset: Optional[int]) -> str:
    """name テーブルの nameID 1 (family)。Windows の英語 → Windows の他 → Mac の順に探す。"""
    if offset is None:
        return ""
    _fmt, count, string_offset = struct.unpack_from(">HHH", data, offset)
    found: dict[int, str] = {}
    for i in range(count):
        platform, encoding, language, name_id, length, str_off = struct.unpack_from(
            ">HHHHHH", data, offset + 6 + 12 * i
        )
        if name_id != 1:
            continue
        raw = data[offset + string_offset + str_off: offset + string_offset + str_off + length]
        if platform == 3:
            rank = 0 if language == 0x409 else 1
            text = raw.decode("utf-16-be", errors="replace")
        elif platform == 1 and encoding == 0:
            rank = 2
            text = raw.decode("mac_roman", errors="replace")
        else:
            continue
        found.setdefault(rank, text)
    return found[min(found)] if found else ""
