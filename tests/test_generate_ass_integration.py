"""VoicevoxClient._generate_ass が出す subtitles.ass の結合テスト

2026-10-11: 字幕の折り返しを文字数 (28 文字) から実フォントの描画幅に替え、長いターンを
連続キューに分けるようにした。ASS ファイルとして崩れていないこと、スタイルは変えていない
こと、分割後もキューが発話区間の中に収まることを確かめる。

2026-10-23: 字幕の下に半透明の帯 (Band スタイルの描画イベント) を敷き、めたんの縁を暗くした。
"""
import re

import pytest

from services.audio_synthesis import subtitle_layout as sl

PRE_ROLL_MS = 2000

LONG_B = (
    "別のエージェントを比べた記事の結びが、今の位置をよく表していますわ。"
    "いずれも完全自律にはまだ遠いが、部分的な自律は確実に実用レベルに達している、というものですの。[src=28] "
    "これはManus、Devin、AutoGPTについての評価で、Hermes Agentを測ったものではありませんけれど。[src=28]"
)
PHRASES = [
    # (audio_segment, start_ms, end_ms, text, speaker)
    (None, 0, 3000, "みなさんこんにちは、ずんだもんなのだ。", "A"),
    (None, 3500, 21500, LONG_B, "B"),
    (None, 22000, 26000, "小さく始めて、記録を見ながら広げる。それがいちばん確かな近道ですわね。", "A"),
]


def _ass_ms(value: str) -> int:
    h, m, s = value.split(":")
    return int(round((int(h) * 3600 + int(m) * 60 + float(s)) * 1000))


@pytest.fixture
def ass_text(tmp_path):
    from services.audio_synthesis.voicevox_client import VoicevoxClient

    # __init__ は AppConfig 必須なので未初期化インスタンスで呼ぶ (_generate_ass は state を使わない)
    client = VoicevoxClient.__new__(VoicevoxClient)
    out = tmp_path / "subtitles.ass"
    client._generate_ass(PHRASES, out)
    return out.read_text(encoding="utf-8")


def _events(ass_text):
    rows = []
    for line in ass_text.splitlines():
        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            rows.append({
                "layer": int(parts[0].split(":")[1]),
                "start": _ass_ms(parts[1]),
                "end": _ass_ms(parts[2]),
                "style": parts[3],
                "text": parts[9],
                "lines": parts[9].split("\\N"),
            })
    return rows


@pytest.fixture
def dialogues(ass_text):
    """字幕のキュー (帯の描画イベントを除く)"""
    return [e for e in _events(ass_text) if e["style"] != "Band"]


@pytest.fixture
def bands(ass_text):
    return [e for e in _events(ass_text) if e["style"] == "Band"]


def test_header_disables_libass_rewrapping(ass_text):
    # 折り返しは \N だけに任せる。libass に再改行させると、検査した行と画面の行がずれる
    assert "WrapStyle: 2" in ass_text
    assert "PlayResX: 1920" in ass_text
    assert "PlayResY: 1080" in ass_text


def test_styles(ass_text):
    # 2026-10-23: めたん (Sub) の縁をピンク (&H00CC99FF) から暗い赤紫に替えた。それ以外は従来どおり
    assert (
        "Style: Main,Meiryo,105,&H0055FF55,&H000000FF,&H00000000,&H80000000,"
        "0,0,0,0,100,100,0,0,1,6,2,2,80,80,30,1"
    ) in ass_text
    assert (
        "Style: Sub,Meiryo,105,&H00FFFFFF,&H000000FF,&H00502080,&H80000000,"
        "0,0,0,0,100,100,0,0,1,6,2,2,80,80,30,1"
    ) in ass_text


def _style_fields(ass_text, name):
    return re.search(rf"^Style: {name},(.+)$", ass_text, re.M).group(1).split(",")


def _luma(ass_colour: str) -> float:
    """&HAABBGGRR の明るさ (0〜255)"""
    value = int(ass_colour[2:], 16)
    r, g, b = value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF
    return 0.299 * r + 0.587 * g + 0.114 * b


@pytest.mark.parametrize("name", ["Main", "Sub"])
def test_outline_is_dark_for_both_speakers(ass_text, name):
    # 明るい背景 (CO2 回の章 1・4) で白文字の輪郭が溶けないよう、縁は暗い色にする
    fields = _style_fields(ass_text, name)
    assert _luma(fields[4]) < 80


def test_speakers_keep_distinct_text_colours(ass_text):
    assert _style_fields(ass_text, "Main")[2] != _style_fields(ass_text, "Sub")[2]


def test_band_style_is_semi_transparent_dark(ass_text):
    fields = _style_fields(ass_text, "Band")
    colour = int(fields[2][2:], 16)
    alpha = colour >> 24
    assert 0 < alpha < 0xFF  # 半透明
    assert _luma(fields[2]) == 0  # 黒
    assert fields[15] == "0" and fields[16] == "0"  # 縁・影なし


def test_every_cue_has_a_band_below_it(dialogues, bands):
    assert len(bands) == len(dialogues)
    for cue, band in zip(dialogues, bands):
        assert (band["start"], band["end"]) == (cue["start"], cue["end"])
        # 帯は字幕より下のレイヤー
        assert band["layer"] < cue["layer"]
        assert band["text"] == sl.band_drawing(len(cue["lines"]))


@pytest.mark.parametrize("n_lines", [1, 2])
def test_band_covers_the_text_block(n_lines):
    text_top = sl.PLAY_RES_Y - sl.MARGIN_V - n_lines * sl.FONT_SIZE
    top = sl.band_top_y(n_lines)
    assert 0 < top < text_top
    assert sl.band_top_y(1) > sl.band_top_y(2)  # 1 行の字幕では帯も低い


def test_layout_constants_match_ass_style(ass_text):
    # subtitle_layout の定数は ASS のスタイル行と同じ値を持つ。ずれたら幅の判定が狂う
    style = re.search(r"^Style: Main,(.+)$", ass_text, re.M).group(1).split(",")
    assert style[0] == sl.FONT_NAME
    assert int(style[1]) == sl.FONT_SIZE
    assert int(style[15]) == sl.OUTLINE
    assert int(style[16]) == sl.SHADOW
    assert int(style[18]) == sl.MARGIN_L
    assert int(style[19]) == sl.MARGIN_R
    assert int(style[20]) == sl.MARGIN_V
    assert int(re.search(r"PlayResY: (\d+)", ass_text).group(1)) == sl.PLAY_RES_Y
    assert int(re.search(r"PlayResX: (\d+)", ass_text).group(1)) == sl.PLAY_RES_X


def test_no_citation_tag_in_subtitles(ass_text):
    assert "[src=" not in ass_text


def test_long_turn_becomes_consecutive_cues(dialogues):
    assert len(dialogues) > len(PHRASES)
    assert all(1 <= len(d["lines"]) <= sl.MAX_LINES for d in dialogues)
    assert all(line.strip() == line and line for d in dialogues for line in d["lines"])


def test_speaker_style_is_kept_for_split_cues(dialogues):
    assert dialogues[0]["style"] == "Main"
    assert dialogues[-1]["style"] == "Main"
    assert {d["style"] for d in dialogues[1:-1]} == {"Sub"}


def test_cue_times_stay_inside_each_utterance(dialogues):
    spans = [(s + PRE_ROLL_MS, e + PRE_ROLL_MS, spk) for _, s, e, _, spk in PHRASES]
    for d in dialogues:
        assert d["start"] <= d["end"]
        assert any(s <= d["start"] and d["end"] <= e for s, e, _ in spans)
    # ターンごとに、先頭キューの開始と末尾キューの終了が発話区間と一致する
    for s, e, _ in spans:
        inside = [d for d in dialogues if s <= d["start"] and d["end"] <= e]
        assert inside[0]["start"] == s
        assert inside[-1]["end"] == e
        for a, b in zip(inside, inside[1:]):
            assert a["end"] == b["start"]


def test_cues_are_in_time_order(dialogues):
    starts = [d["start"] for d in dialogues]
    assert starts == sorted(starts)


@pytest.mark.skipif(sl.find_subtitle_font() is None, reason="Meiryo が無い環境では描画幅を測れない")
def test_all_lines_fit_safe_area(dialogues):
    measure = sl.make_font_measurer(sl.find_subtitle_font())
    for d in dialogues:
        for line in d["lines"]:
            assert measure(line) <= sl.MAX_TEXT_WIDTH_PX


def test_phrase_data_is_not_mutated(tmp_path):
    from services.audio_synthesis.voicevox_client import VoicevoxClient

    before = list(PHRASES)
    VoicevoxClient.__new__(VoicevoxClient)._generate_ass(PHRASES, tmp_path / "subtitles.ass")
    assert PHRASES == before
    assert "[src=28]" in PHRASES[1][3]
