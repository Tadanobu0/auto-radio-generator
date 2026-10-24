"""章タイトルの帯が右上の日付と重ならない (010) のテスト

2026-10-24 (Wi-Fi の回): 横に長い題 (「電波はどこで弱まる？ 距離・壁・水と金属」
「まとめ: 仕組みを知れば置き場所は変えられる」) の帯が、右上の日付 (2026/10/24) と少し
重なっていた。帯は y=40 から、日付は y=20 から描かれ、帯の最大幅 1700px だと右端が
x=1820 まで来る。帯の最大幅を日付の幅 + 間隔のぶん左右とも狭めた (1480px)。

ffmpeg を呼ぶテストは、ffmpeg と Meiryo のある環境 (Windows 実機) だけで動く。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from services.audio_synthesis import subtitle_layout as sl
from services.video_rendering import topic_overlay as to
from services.video_rendering.video_track_renderer import VideoTrackRenderer

LONG_TITLES = [
    "電波はどこで弱まる？ 距離・壁・水と金属",
    "まとめ: 仕組みを知れば置き場所は変えられる",
]


def _box_right_edge(title: str) -> float:
    size, lines = to.layout_title(title)
    measure = to._title_measurer(size)
    return to.FRAME_WIDTH / 2 + max(measure(line) for line in lines) / 2 + to.TITLE_BOX_BORDER


def test_title_max_width_leaves_room_for_the_date():
    assert to.TITLE_MAX_WIDTH_PX == 1480
    date_left = to.FRAME_WIDTH - to.DATE_MARGIN_X - to.DATE_MAX_WIDTH_PX
    assert to.FRAME_WIDTH / 2 + to.TITLE_MAX_WIDTH_PX / 2 + to.TITLE_BOX_BORDER <= date_left - to.TITLE_DATE_GAP


@pytest.mark.parametrize("title", LONG_TITLES)
def test_long_titles_stay_left_of_the_date(title):
    date_left = to.FRAME_WIDTH - to.DATE_MARGIN_X - to.DATE_MAX_WIDTH_PX
    assert _box_right_edge(title) <= date_left - to.TITLE_DATE_GAP


def test_question_mark_with_space_splits_title_and_subtitle():
    size, lines = to.layout_title(LONG_TITLES[0])
    assert lines == ["電波はどこで弱まる？", "距離・壁・水と金属"]
    assert size == to.TITLE_FONT_SIZES[0]


def test_colon_title_splits_after_colon():
    size, lines = to.layout_title(LONG_TITLES[1])
    assert lines == ["まとめ:", "仕組みを知れば置き場所は変えられる"]
    assert size == to.TITLE_FONT_SIZES[0]


def test_question_mark_without_space_is_not_a_break():
    assert to._split_at_colon("なぜ？の答え") is None


def test_date_filter_uses_shared_position():
    renderer = VideoTrackRenderer.__new__(VideoTrackRenderer)
    date = renderer._build_date_filter()
    assert f":fontsize={to.DATE_FONT_SIZE}:" in date
    assert f":x=w-tw-{to.DATE_MARGIN_X}:y={to.DATE_MARGIN_Y}:" in date


# ---------------------------------------------------------------- ffmpeg で描いて重なりを見る

def _find_ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return found
    repo = Path(__file__).resolve().parents[1]
    for candidate in (repo.parent / "ffmpeg.exe", repo.parent.parent / "ffmpeg.exe"):
        if candidate.exists():
            return str(candidate)
    return None


FFMPEG = _find_ffmpeg()
FONT = sl.find_subtitle_font()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None or FONT is None, reason="ffmpeg と Meiryo のある環境だけ")
FRAME_H = 400
GRAY = 128


def _changed_pixels(vf: str):
    """灰色の画面に vf を当てた 1 コマで、地の灰色から変わった画素の (行, 列) の配列。"""
    import numpy as np

    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=0x808080:s={to.FRAME_WIDTH}x{FRAME_H}:r=1:d=1",
        "-vf", vf, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-400:]
    frame = np.frombuffer(proc.stdout, dtype=np.uint8).reshape(FRAME_H, to.FRAME_WIDTH).astype(int)
    return np.argwhere(np.abs(frame - GRAY) > 8)


def test_real_date_text_fits_its_reserved_width():
    if FONT is None:
        pytest.skip("Meiryo のある環境だけ")
    measure = to._title_measurer(to.DATE_FONT_SIZE)
    assert measure("2026/10/24") <= to.DATE_MAX_WIDTH_PX - 4  # 影 (shadowx=2) の分も残す


@needs_ffmpeg
@pytest.mark.parametrize("title", LONG_TITLES)
def test_drawn_title_band_does_not_touch_the_date(title):
    renderer = VideoTrackRenderer.__new__(VideoTrackRenderer)
    fontfile = renderer._escape_windows_path(str(FONT))
    title_vf = ",".join(to.build_title_drawtexts(title, 0.0, 10.0, fontfile, renderer._escape_drawtext_text))
    date_vf = renderer._build_date_filter().replace(renderer._get_drawtext_fontfile(), fontfile)

    band = _changed_pixels(title_vf)
    date = _changed_pixels(date_vf)
    assert len(band) and len(date)
    date_top, date_bottom = date[:, 0].min(), date[:, 0].max()
    date_left = date[:, 1].min()
    # 日付と同じ高さにある帯の画素は、日付の左端より 10px 以上左
    beside = band[(band[:, 0] >= date_top) & (band[:, 0] <= date_bottom)]
    assert len(beside) > 0  # 帯は日付と同じ高さにかかっている (横で避けている)
    assert beside[:, 1].max() < date_left - 10
