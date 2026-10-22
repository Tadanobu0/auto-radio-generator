"""字幕の分割・改行の規則のテスト (services/audio_synthesis/subtitle_layout.py)

2026-10-23: CO2 回のコマ評価 (radio_director-012) で見つかった問題への回帰テスト。
  - 「、」で終わる字幕が多く、数語だけの字幕 (「と説明していますの。」) が単独で出ていた
  - 「…抑えるには、」で切れて数字が次の字幕に送られていた
  - 3 行の字幕が 21% あった
  - 改行が語の途中に来ていた (行頭に「いう」「もの」「して」、「CO2その/ものは」「頭の/テスト」)

規則: なるべく「。」で分ける / 「、」で分けるのは 1 文が 2 行を超えるときだけ / 「、」で
分けた残りが短いなら前後に繋げる / 1 枚 2 行まで / 行頭に助詞・「いう」「もの」「して」「こと」を置かない。
"""
import re

import pytest

from services.audio_synthesis import subtitle_layout as sl

FONT_PATH = sl.find_subtitle_font()

BAD_LINE_HEAD_WORDS = ("いう", "もの", "して", "こと")
BAD_LINE_HEAD_PARTICLES = "はがをにでともへやのか"
PUNCT_END = "、。，．！？!?,.」』）)】"


@pytest.fixture(scope="module")
def measure():
    if FONT_PATH is not None:
        return sl.make_font_measurer(FONT_PATH)
    return sl.estimate_width


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _longest(head: str, tail: str, n_lines: int, measure, suffix: str = "") -> str:
    """head + "て"*k + tail に suffix を足しても n_lines 行に収まる、最大の k の文を返す。"""
    best = None
    for k in range(0, 400):
        sentence = head + "て" * k + tail
        if len(sl.wrap_lines(sentence + suffix, measure)) > n_lines:
            break
        best = sentence
    assert best is not None, "head + tail だけで行数を超える"
    return best


def test_cue_is_at_most_two_lines():
    assert sl.MAX_LINES == 2


# ---------------------------------------------------------------------------
# 分割の規則
# ---------------------------------------------------------------------------
class TestSplitRules:
    def test_sentences_that_fit_are_not_split_at_commas(self, measure):
        # どの文も 2 行に収まるなら「、」では分けず、「。」だけで分ける
        sentence = _longest("これは、", "、大事な話ですの。", sl.MAX_LINES, measure)
        assert len(sl.wrap_lines(sentence + sentence, measure)) > sl.MAX_LINES
        cues = sl.split_cues(sentence * 3, measure)
        assert cues == [sentence] * 3

    def test_comma_split_does_not_leave_short_tail(self, measure):
        # 「A、B、C。」で A、B、 が 2 行ちょうど、C が短いとき、
        # 旧実装は [A、B、][C。] と分けて C (7 字) が単独で出ていた。[A、][B、C。] にする
        b = "その次に来るのは、"
        c = "と思いますの。"
        ab = _longest("最初に", "けれど、" + b, sl.MAX_LINES, measure)
        a = ab[: -len(b)]
        text = ab + c
        assert len(sl.wrap_lines(text, measure)) > sl.MAX_LINES
        assert len(c) < sl.MIN_CUE_CHARS
        cues = sl.split_cues(text, measure)
        assert cues == [a, b + c]

    def test_quote_and_its_tail_stay_together(self, measure):
        # 「…。」と説明していますの。 の「と説明していますの。」を単独の字幕にしない
        quote = "「排出量はまだ減らせる。」"
        tail = "と説明していますの。"
        first = _longest("まず", "ですわ。", sl.MAX_LINES, measure, suffix=quote)
        text = first + quote + tail
        assert len(sl.wrap_lines(text, measure)) > sl.MAX_LINES
        cues = sl.split_cues(text, measure)
        assert cues == [first, quote + tail]

    def test_long_sentence_is_split_at_commas_into_two_line_cues(self, measure):
        clause = "排出を抑えるには工夫が必要で、"
        text = clause * 6 + "数字で見ると三割ほど減りますの。"
        cues = sl.split_cues(text, measure)
        assert len(cues) >= 2
        assert all(len(sl.wrap_lines(c, measure)) <= sl.MAX_LINES for c in cues)
        assert all(c.endswith("、") for c in cues[:-1])
        assert all(len(_squash(c)) >= sl.MIN_CUE_CHARS for c in cues)
        assert _squash("".join(cues)) == _squash(text)

    def test_sentence_end_is_preferred_over_comma(self, measure):
        # 2 文目の途中の「、」で区切って 1 文目に寄せるより、文末で区切る
        first = _longest("はじめに", "ですの。", 1, measure)
        second = _longest("そして、", "なのだ。", sl.MAX_LINES, measure)
        cues = sl.split_cues(first + second, measure)
        assert all(c.endswith("。") for c in cues)
        assert _squash("".join(cues)) == _squash(first + second)


# ---------------------------------------------------------------------------
# 改行の位置
# ---------------------------------------------------------------------------
LINE_HEAD_TEXTS = [
    "CO2そのものは悪者ではないということを、頭のテストで確かめてみるのだ。",
    "温室効果ガスというものは、排出を抑えるにはいろいろな工夫が必要なものですわね。",
    "技術を活用して排出を減らすことが、いちばん確かな近道だと説明していますの。",
    "大気中の二酸化炭素の濃度というものは、産業革命のころと比べて大きく増えているのだ。",
]


def _assert_good_line_heads(lines):
    for prev, line in zip(lines, lines[1:]):
        if prev[-1:] in PUNCT_END:
            continue
        assert not line.startswith(BAD_LINE_HEAD_WORDS), (prev, line)
        assert line[:1] not in BAD_LINE_HEAD_PARTICLES, (prev, line)


class TestLineBreaks:
    @pytest.mark.parametrize("text", LINE_HEAD_TEXTS)
    def test_phrase_does_not_start_with_particle_or_bound_word(self, measure, text):
        phrases = sl._phrases(text, measure, sl.MAX_TEXT_WIDTH_PX)
        assert "".join(phrases) == text
        _assert_good_line_heads(phrases)

    def test_sono_mono_is_not_broken(self, measure):
        phrases = sl._phrases("CO2そのものは悪者ではない。", measure, sl.MAX_TEXT_WIDTH_PX)
        assert not any(p.startswith("もの") for p in phrases)

    @pytest.mark.parametrize("text", LINE_HEAD_TEXTS)
    @pytest.mark.parametrize("pad", range(0, 24, 3))
    def test_wrapped_lines_do_not_start_badly(self, measure, text, pad):
        # 前置きの長さを変えて、改行位置がいろいろな所に来るようにする
        body = "ア" * pad + "。" + text * 2
        for cue in sl.split_cues(body, measure):
            _assert_good_line_heads(sl.wrap_lines(cue, measure))

    def test_break_after_no_is_avoided_when_balance_allows(self):
        # 「頭の/テスト」で切ると幅は最も揃うが、少し崩してでも「の」の直後は避ける
        measure = lambda s: 10.0 * len(s)
        lines = sl._balanced_lines(["あいうえ", "頭の", "テストかな"], 2, measure, 100)
        assert lines == ["あいうえ", "頭のテストかな"]

    def test_break_after_no_is_used_when_nothing_else_fits(self):
        measure = lambda s: 10.0 * len(s)
        lines = sl._balanced_lines(["あ", "頭の", "テストかなさしす"], 2, measure, 100)
        assert lines == ["あ頭の", "テストかなさしす"]
