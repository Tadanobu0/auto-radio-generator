"""章タイトルの「：」での折り返しと、「%」を含む題の描画のテスト

2026-10-23 (radio_director-013 のコマ評価):
  - 「家のHDDに当てはめる：1台の確率と前ぶれと備え」を 2 行に折ると 2 行目の頭に「：」が来た
  - deep_dive_0 の章タイトルが 30 秒間隔のコマ (2m32s・4m29s) に出ていなかった。drawtext は
    既定 (expansion=normal) で「%」を展開記法として読み、「%{」以外が続くと文字を描かない

ffmpeg を呼ぶテストは、ffmpeg と Meiryo のある環境 (Windows 実機) だけで動く。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from core.interfaces import ChapterMarker
from services.audio_synthesis import subtitle_layout as sl
from services.video_rendering import topic_overlay as to
from services.video_rendering.video_track_renderer import VideoTrackRenderer

HDD_TITLE = "家のHDDに当てはめる：1台の確率と前ぶれと備え"


def _fixed_width(size):
    return lambda s: size * len(s)


# ---------------------------------------------------------------------------
# 「：」「: 」での折り返し
# ---------------------------------------------------------------------------
def test_hdd_title_breaks_after_full_width_colon():
    size, lines = to.layout_title(HDD_TITLE)
    assert lines == ["家のHDDに当てはめる：", "1台の確率と前ぶれと備え"]
    assert size == to.TITLE_FONT_SIZES[0]


@pytest.mark.parametrize("title, expected", [
    ("GLMとは何者か：355Bの巨大モデルから9Bの小型モデルまで",
     ["GLMとは何者か：", "355Bの巨大モデルから9Bの小型モデルまで"]),
    ("データを外に出さない現場：銀行とメーカーはどう組んだか",
     ["データを外に出さない現場：", "銀行とメーカーはどう組んだか"]),
    ("家のPCでGLMを動かす：必要なVRAM・RAMとソフトの選び方",
     ["家のPCでGLMを動かす：", "必要なVRAM・RAMとソフトの選び方"]),
])
def test_real_chapter_titles_break_at_colon(title, expected):
    # 20261006_llmglm_frontier の章タイトル (実物)
    assert to.layout_title(title)[1] == expected


def test_ascii_colon_with_space_is_a_break_and_colon_stays_on_first_line():
    title = "イントロ: " + "あ" * 25
    size, lines = to.layout_title(title, _fixed_width, 1700)
    assert lines == ["イントロ:", "あ" * 25]


def test_ascii_colon_without_space_is_not_a_break():
    # 「10:30」のような時刻は区切りにしない
    title = "朝10:30からの" + "あ" * 20
    assert to._split_at_colon(title) is None


def test_short_title_with_colon_stays_on_one_line():
    size, lines = to.layout_title("GLM：概要")
    assert lines == ["GLM：概要"]
    assert size == to.TITLE_FONT_SIZES[0]


def test_colon_split_shrinks_font_before_falling_back():
    # 80px では副題が 1 行に入らないが、64px なら主題・副題とも 1 行に入る
    title = "あ" * 5 + "：" + "い" * 26
    size, lines = to.layout_title(title, _fixed_width, 1700)
    assert (size, lines) == (64, ["あ" * 5 + "：", "い" * 26])


def test_colon_split_that_never_fits_falls_back_to_measured_wrap():
    title = "あ" * 3 + "：" + "い" * 40
    size, lines = to.layout_title(title, _fixed_width, 1700)
    assert len(lines) <= to.TITLE_MAX_LINES
    assert "".join(lines) == title
    assert not any(line.startswith(("：", ":")) for line in lines)


def test_colon_never_starts_a_line_in_fallback_wrap():
    lines = to._keep_colon_at_line_end(["家のHDDに当てはめる", "：1台の確率と前ぶれと備え"], _fixed_width(80), 1700)
    assert lines == ["家のHDDに当てはめる：", "1台の確率と前ぶれと備え"]


def test_empty_head_or_tail_is_not_split():
    assert to._split_at_colon("：副題だけ") is None
    assert to._split_at_colon("主題だけ：") is None


# ---------------------------------------------------------------------------
# 「%」を含む題
# ---------------------------------------------------------------------------
def test_title_drawtext_disables_text_expansion():
    for drawtext in to.build_title_drawtexts("年間故障率1.5%の意味", 0.0, 10.0, "font", lambda s: s):
        assert ":expansion=none:" in drawtext


def test_every_chapter_gets_expansion_none():
    chapters = [
        ChapterMarker(start_time_sec=0.0, title="はじめに", section_id="intro"),
        ChapterMarker(start_time_sec=30.0, title="市場の急成長と「40%の失敗」パラドックス", section_id="deep_dive"),
    ]
    overlay = to.build_topic_overlay_filter(chapters, 90.0, "font", lambda s: s)
    assert overlay.count("drawtext=") == overlay.count(":expansion=none:")


def _find_ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return found
    repo = Path(__file__).resolve().parents[1]
    # 本番は auto_radio_generator の 1 つ上、試験 clone は 2 つ上に ffmpeg.exe がある
    for candidate in (repo.parent / "ffmpeg.exe", repo.parent.parent / "ffmpeg.exe"):
        if candidate.exists():
            return str(candidate)
    return None


FFMPEG = _find_ffmpeg()
FONT = sl.find_subtitle_font()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None or FONT is None, reason="ffmpeg と Meiryo のある環境だけ")


def _renderer():
    return VideoTrackRenderer.__new__(VideoTrackRenderer)


def _title_filter(title: str) -> str:
    renderer = _renderer()
    fontfile = renderer._escape_windows_path(str(FONT))
    return ",".join(to.build_title_drawtexts(title, 0.0, 10.0, fontfile, renderer._escape_drawtext_text))


def _brightest_pixel(vf: str) -> tuple[int, int]:
    """黒い画面に vf を当てた 1 コマ目の、最も明るい画素 (0〜255) と ffmpeg の終了コード。"""
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=1920x400:r=1:d=1",
        "-vf", vf, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    return proc.returncode, (max(proc.stdout) if proc.stdout else 0)


@needs_ffmpeg
@pytest.mark.parametrize("title", [
    "年間故障率1.5%の意味",
    "市場の急成長と「40%の失敗」パラドックス",
    HDD_TITLE,
    "イントロ: 10:30, AIの見方",
])
def test_title_is_drawn_by_ffmpeg(title):
    code, brightest = _brightest_pixel(_title_filter(title))
    assert code == 0
    assert brightest > 200  # 白い文字が描かれている (帯は黒の半透明なので黒のまま)


@needs_ffmpeg
def test_default_expansion_drops_title_with_percent():
    # 原因の確認: expansion=none を外すと「%」を含む題は描かれない (または ffmpeg が失敗する)
    vf = _title_filter("年間故障率1.5%の意味").replace(":expansion=none", "")
    code, brightest = _brightest_pixel(vf)
    assert code != 0 or brightest < 128
