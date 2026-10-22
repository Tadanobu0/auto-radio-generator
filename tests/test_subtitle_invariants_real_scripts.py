"""実台本に対する字幕の不変条件テスト

tests/fixtures/real_scripts/ の verified_script.json (無改変の実物、選定理由は同フォルダの
README.md) の全ターンを字幕化して、次を確かめる:

  1. 全キューの全行が、実フォントの描画幅で安全域 (左右マージン込み) に収まる
  2. 1 キューは MAX_LINES (2) 行以内
  3. [src=N] が字幕に残らない
  4. 文字が欠けない / キューが発話区間をはみ出さない

1 は Meiryo が要るので、無い環境 (Mac) では skip する。
"""
import json
import re
from pathlib import Path

import pytest

from services.audio_synthesis import subtitle_layout as sl

REAL_SCRIPTS_DIR = Path(__file__).parent / "fixtures" / "real_scripts"
REAL_SCRIPTS = sorted(REAL_SCRIPTS_DIR.glob("*.json"))
HERMES = REAL_SCRIPTS_DIR / "20261007_hermesagent_frontier.json"

FONT_PATH = sl.find_subtitle_font()
needs_font = pytest.mark.skipif(FONT_PATH is None, reason="Meiryo が無い環境では描画幅を測れない")

# 発話区間は実測値を持たないので、読み上げ速度の目安 (1 文字 130ms) で仮に置く
MS_PER_CHAR = 130


def _turn_texts(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    script = data.get("script", data)
    return [turn["text"] for seg in script["segments"] for turn in seg["turns"]]


def _measure():
    if FONT_PATH is not None:
        return sl.make_font_measurer(FONT_PATH)
    return sl.estimate_width


def _layout_script(path: Path):
    """[(ターン本文, 開始ms, 終了ms, キュー列)] を返す。"""
    measure = _measure()
    out = []
    t = 2000
    for text in _turn_texts(path):
        end = t + max(1, len(text)) * MS_PER_CHAR
        out.append((text, t, end, sl.layout_turn(text, t, end, measure)))
        t = end + 500
    return out


@pytest.fixture(scope="module", params=REAL_SCRIPTS, ids=lambda p: p.stem)
def laid_out(request):
    return _layout_script(request.param)


def test_fixtures_exist():
    assert len(REAL_SCRIPTS) == 10
    assert HERMES in REAL_SCRIPTS


@needs_font
def test_every_line_fits_safe_area(laid_out):
    measure = _measure()
    too_wide = [
        (round(measure(line)), line)
        for _, _, _, cues in laid_out
        for cue in cues
        for line in cue.lines
        if measure(line) > sl.MAX_TEXT_WIDTH_PX
    ]
    assert too_wide == []


def test_every_cue_within_line_cap(laid_out):
    too_tall = [cue.lines for _, _, _, cues in laid_out for cue in cues if not 1 <= len(cue.lines) <= sl.MAX_LINES]
    assert too_tall == []


def test_no_citation_tag_left(laid_out):
    left = [cue.text for _, _, _, cues in laid_out for cue in cues if "[src=" in cue.text]
    assert left == []


def test_no_character_lost(laid_out):
    squash = lambda s: re.sub(r"\s+", "", s)
    for text, _, _, cues in laid_out:
        assert squash("".join(c.text for c in cues)) == squash(sl.strip_for_display(text))


def test_other_brackets_are_not_removed(laid_out):
    # [src=N] 以外の角括弧 ([3] [A] [medium] など) は今回の対象外。数が変わらないこと
    other = re.compile(r"\[(?!src=)[^\]]*\]")
    for text, _, _, cues in laid_out:
        assert len(other.findall("".join(c.text for c in cues))) == len(other.findall(text))


def test_cues_tile_the_utterance(laid_out):
    for text, start, end, cues in laid_out:
        if not cues:
            assert sl.strip_for_display(text) == ""
            continue
        assert cues[0].start_ms == start
        assert cues[-1].end_ms == end
        for a, b in zip(cues, cues[1:]):
            assert a.end_ms == b.start_ms
        assert all(c.start_ms <= c.end_ms for c in cues)


def test_short_cues_only_remain_when_merge_is_impossible(laid_out):
    measure = _measure()
    for _, _, _, cues in laid_out:
        for i, cue in enumerate(cues):
            if cue.end_ms - cue.start_ms >= sl.MIN_CUE_MS or len(cues) == 1:
                continue
            for j in (i - 1, i + 1):
                if 0 <= j < len(cues):
                    lo, hi = sorted((i, j))
                    merged = cues[lo].text + cues[hi].text
                    assert len(sl.wrap_lines(merged, measure)) > sl.MAX_LINES


# ---------------------------------------------------------------------------
# 分割と改行の規則 (2026-10-23, radio_director-012 のコマ評価)
# ---------------------------------------------------------------------------
_SENTENCE_STOP = "。！？!?"
_PUNCT_END = "、。，．！？!?,.」』）)】"


def test_comma_split_only_when_sentence_exceeds_cap(laid_out):
    # 「、」で終わるキューは、その文が MAX_LINES 行を超えるときだけ
    # キューの text は行を空白なしで繋いだものなので、空白以外の文字数で表示テキスト上の位置を求める
    measure = _measure()
    bad = []
    for text, _, _, cues in laid_out:
        display = sl.strip_for_display(text)
        offsets = [i + 1 for i, ch in enumerate(display) if not ch.isspace()]
        n = 0
        for cue in cues[:-1]:  # ターン自体が「、」で終わる (次のターンに続く) ものは分割ではない
            n += len(re.sub(r"\s+", "", cue.text))
            if not cue.text.endswith(("、", "，", ",")):
                continue
            pos = offsets[n - 1]
            start = max(display.rfind(ch, 0, pos) for ch in _SENTENCE_STOP) + 1
            ends = [display.find(ch, pos) for ch in _SENTENCE_STOP]
            end = min([e for e in ends if e >= 0], default=len(display) - 1) + 1
            sentence = display[start:end].strip()
            if "「" not in sentence and len(sl.wrap_lines(sentence, measure)) <= sl.MAX_LINES:
                bad.append(sentence)
    assert bad == []


def test_short_tail_after_comma_only_when_merge_is_impossible(laid_out):
    measure = _measure()
    bad = []
    for _, _, _, cues in laid_out:
        for prev, cue in zip(cues, cues[1:]):
            if prev.text.endswith(("、", "，", ",")) and len(re.sub(r"\s+", "", cue.text)) < sl.MIN_CUE_CHARS:
                if len(sl.wrap_lines(prev.text + cue.text, measure)) <= sl.MAX_LINES:
                    bad.append((prev.text, cue.text))
    assert bad == []


def test_no_line_starts_with_particle_or_bound_word(laid_out):
    bad = []
    for _, _, _, cues in laid_out:
        for cue in cues:
            for prev, line in zip(cue.lines, cue.lines[1:]):
                if prev[-1:] in _PUNCT_END:
                    continue
                # 助詞と同じ字で始まる自立語 (「できる」など) は sl._starts_badly が除く
                if sl._starts_badly(line):
                    bad.append((prev, line))
    assert bad == []


# ---------------------------------------------------------------------------
# Hermes Agent 回 (run 20261007_190721) で実際に崩れていたキューの回帰
#   #2  : 5 行 + [src=4] [src=23] が表示
#   #70 : 7 行、2 行目が左右で欠け、[src=28] が 2 回表示
#   #81 : 1 行目 (28 文字 = 1960px) が左右で欠け
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("turn_no", [2, 70, 81])
def test_hermes_broken_cues_are_fixed(turn_no):
    measure = _measure()
    text = _turn_texts(HERMES)[turn_no - 1]
    cues = sl.layout_turn(text, 0, len(text) * MS_PER_CHAR, measure)
    assert cues
    for cue in cues:
        assert len(cue.lines) <= sl.MAX_LINES
        assert "[src=" not in cue.text
        if FONT_PATH is not None:
            assert max(measure(line) for line in cue.lines) <= sl.MAX_TEXT_WIDTH_PX


def test_hermes_turn_2_and_70_had_tags():
    # fixture が無改変であること (タグは台本側に残っている) の確認
    texts = _turn_texts(HERMES)
    assert "[src=4]" in texts[1] and "[src=23]" in texts[1]
    assert texts[69].count("[src=28]") == 2
