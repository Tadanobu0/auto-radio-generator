"""字幕の表示層 (services/audio_synthesis/subtitle_layout.py) の単体テスト

2026-10-11: 公開動画 (Hermes Agent 回) で字幕が画面の左右にはみ出し、1 キューが最大 7 行に
なり、[src=N] がそのまま表示されていた問題への回帰テスト。

幅の判定は実フォント (Meiryo) の描画幅で行う。Meiryo が無い環境 (Mac) では幅のテストだけ
skip し、それ以外は概算幅で動かす。
"""
import re

import pytest

from services.audio_synthesis import subtitle_layout as sl

FONT_PATH = sl.find_subtitle_font()
needs_font = pytest.mark.skipif(FONT_PATH is None, reason="Meiryo が無い環境では描画幅を測れない")


@pytest.fixture(scope="module")
def measure():
    if FONT_PATH is not None:
        return sl.make_font_measurer(FONT_PATH)
    return sl.estimate_width


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _grow_until(prefix: str, head: str, tail: str, n_lines: int, measure) -> str:
    """prefix を付けたときにちょうど n_lines 行になる最短の 1 文 (head + 伸ばし + tail) を返す。"""
    for n in range(0, 400):
        sentence = head + "て" * n + tail
        if len(sl.wrap_lines(prefix + sentence, measure)) >= n_lines:
            return sentence
    raise AssertionError("n_lines 行に届かない")


# ---------------------------------------------------------------------------
# [src=N] の除去
# ---------------------------------------------------------------------------
class TestStripForDisplay:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            # 文末
            ("挙がる存在ですわね。[src=23]", "挙がる存在ですわね。"),
            # 文中 (タグの後ろに空白)
            ("ですの。[src=4] 自分のマシン側に", "ですの。自分のマシン側に"),
            # タグの前後に空白
            ("ですの。 [src=4] 自分の", "ですの。自分の"),
            # 連続タグ
            ("と報告されています。[src=3][src=12]", "と報告されています。"),
            ("と報告されています。[src=3] [src=12] 次に", "と報告されています。次に"),
            # 句読点の前のタグ
            ("増えました[src=7]。次の話題です。", "増えました。次の話題です。"),
            ("増えました [src=7] 。次の話題です。", "増えました。次の話題です。"),
            # タグを挟んで句読点が重なる
            ("増えました。[src=7]。次の話題です。", "増えました。次の話題です。"),
            # 英単語の間: 空白は 1 つ残す
            ("GPT [src=1] and Claude", "GPT and Claude"),
            # 英数字に空白なしで挟まれたタグ: 空白を足さない
            ("v2[src=1]beta", "v2beta"),
            # 先頭・末尾
            ("[src=1] 最初からタグ", "最初からタグ"),
            ("タグだけ残る [src=1] ", "タグだけ残る"),
        ],
    )
    def test_removes_tag_and_tidies(self, raw, expected):
        assert sl.strip_for_display(raw) == expected

    def test_text_without_tag_is_unchanged(self):
        text = "どうなるの？ 変な癖がついたまま、自信満々で働く新人さん。"
        assert sl.strip_for_display(text) == text

    def test_other_brackets_are_kept(self):
        # 今回の対象は [src=N] だけ。[3] や [A] などは触らない
        text = "調査では[3]と[A][medium]の記載があります。"
        assert sl.strip_for_display(text) == text

    def test_only_tag_becomes_empty(self):
        assert sl.strip_for_display(" [src=5] ") == ""


# ---------------------------------------------------------------------------
# 折り返し
# ---------------------------------------------------------------------------
class TestWrapLines:
    def test_empty(self, measure):
        assert sl.wrap_lines("", measure) == []
        assert sl.wrap_lines("   ", measure) == []

    def test_short_text_is_one_line(self, measure):
        assert sl.wrap_lines("こんにちは。", measure) == ["こんにちは。"]

    def test_every_line_within_given_width(self, measure):
        text = "小さく始めて、記録を見ながら広げる。それがいちばん確かな近道ですわね。" * 3
        lines = sl.wrap_lines(text, measure)
        assert len(lines) > 1
        assert all(measure(line) <= sl.MAX_TEXT_WIDTH_PX for line in lines)
        assert _squash("".join(lines)) == _squash(text)

    def test_single_piece_wider_than_limit_is_split(self, measure):
        # 文節として割れない塊でも、幅を超えたら文字単位で割る (旧実装は素通しだった)
        text = "a" * 200
        lines = sl.wrap_lines(text, measure)
        assert len(lines) > 1
        assert all(measure(line) <= sl.MAX_TEXT_WIDTH_PX for line in lines)
        assert "".join(lines) == text

    def test_no_leading_or_trailing_space(self, measure):
        text = "This is a very long English sentence that should wrap at word boundaries okay " * 2
        for line in sl.wrap_lines(text, measure):
            assert line == line.strip()

    def test_english_wraps_at_word_boundary(self, measure):
        text = "This is a very long English sentence that should wrap at word boundaries rather than inside words."
        words = set(text.split())
        for line in sl.wrap_lines(text, measure):
            assert set(line.split()) <= words

    def test_line_does_not_start_with_punctuation(self, measure):
        text = "あ" * 30 + "、" + "い" * 30 + "。" + "う" * 30 + "」"
        for line in sl.wrap_lines(text, measure):
            assert line[0] not in "、。」"

    def test_lines_are_balanced(self, measure):
        # 末尾に 1〜2 文字だけの行を残さない (旧実装では「よ。」だけの行ができていた)
        text = "AIエージェントが秘書の代わりになる日は来るのか、というところを一緒に考えていくよ。"
        lines = sl.wrap_lines(text, measure)
        assert len(lines) == 2
        assert len(lines[-1]) > 5

    @needs_font
    def test_measured_width_of_fullwidth_char(self, measure):
        # Meiryo 105 (ASS の Fontsize) は em 70px。libass と同じ換算になっていること
        assert measure("あ") == pytest.approx(70.0, abs=0.5)
        assert measure("あ" * 28) > sl.PLAY_RES_X  # 旧実装の上限 28 文字は画面幅を超える

    @needs_font
    def test_old_overflowing_line_is_now_wrapped(self, measure):
        # Hermes 回 cue#81 の 1 行目 (28 文字 = 1960px) が安全域に収まる形で折り返される
        text = "小さく始めて、記録を見ながら広げる。それがいちばん確かな近道ですわね。"
        lines = sl.wrap_lines(text, measure)
        assert max(measure(line) for line in lines) <= sl.MAX_TEXT_WIDTH_PX


# ---------------------------------------------------------------------------
# キュー分割
# ---------------------------------------------------------------------------
LONG_TURN = (
    "別のエージェントを比べた記事の結びが、今の位置をよく表していますわ。"
    "いずれも完全自律にはまだ遠いが、部分的な自律は確実に実用レベルに達している、というものですの。"
    "これはManus、Devin、AutoGPTについての評価で、Hermes Agentを測ったものではありませんけれど。"
)


class TestSplitCues:
    def test_empty(self, measure):
        assert sl.split_cues("", measure) == []

    def test_short_turn_is_single_cue(self, measure):
        assert sl.split_cues("そうなんだね。", measure) == ["そうなんだね。"]

    def test_long_turn_is_split_within_line_cap(self, measure):
        cues = sl.split_cues(LONG_TURN, measure)
        assert len(cues) >= 2
        for cue in cues:
            assert 1 <= len(sl.wrap_lines(cue, measure)) <= sl.MAX_LINES

    def test_no_character_is_lost(self, measure):
        cues = sl.split_cues(LONG_TURN, measure)
        assert _squash("".join(cues)) == _squash(LONG_TURN)

    def test_sentences_are_grouped_up_to_preferred_lines(self, measure):
        # 短い文は 2 行に収まる範囲でまとめる (1 文ごとに細切れにしない)
        text = "はい。そうです。わかりました。"
        assert sl.split_cues(text, measure) == [text]

    def test_sentence_longer_than_cap_is_split_at_commas(self, measure):
        clause = "これは読点で区切られたそれなりに長い節であり、"
        text = clause * 8 + "最後にようやく文が終わりますの。"
        cues = sl.split_cues(text, measure)
        assert len(cues) >= 2
        assert all(len(sl.wrap_lines(c, measure)) <= sl.MAX_LINES for c in cues)
        # 読点の直後で切れている
        assert all(c.endswith("、") for c in cues[:-1])
        assert _squash("".join(cues)) == _squash(text)

    def test_text_without_any_punctuation_is_split_by_lines(self, measure):
        text = "句読点がひとつもないまま延々と続いてしまう文章でも行数の上限は必ず守られる" * 6
        cues = sl.split_cues(text, measure)
        assert len(cues) >= 2
        assert all(len(sl.wrap_lines(c, measure)) <= sl.MAX_LINES for c in cues)
        assert _squash("".join(cues)) == _squash(text)

    @needs_font
    def test_all_lines_within_safe_area(self, measure):
        for cue in sl.split_cues(LONG_TURN, measure):
            for line in sl.wrap_lines(cue, measure):
                assert measure(line) <= sl.MAX_TEXT_WIDTH_PX


# ---------------------------------------------------------------------------
# 時間配分
# ---------------------------------------------------------------------------
class TestAllocateTimes:
    def test_empty(self):
        assert sl.allocate_times([], 0, 1000) == []

    def test_single_cue_gets_whole_span(self):
        assert sl.allocate_times(["あいう"], 2000, 5000) == [(2000, 5000)]

    def test_proportional_to_character_count(self):
        times = sl.allocate_times(["あ" * 10, "い" * 30], 0, 8000)
        assert times == [(0, 2000), (2000, 8000)]

    def test_covers_span_without_gap_or_overlap(self):
        cues = ["あ" * 7, "い" * 13, "う" * 5, "え" * 21]
        times = sl.allocate_times(cues, 12340, 23450)
        assert times[0][0] == 12340
        assert times[-1][1] == 23450
        for (_, end), (start, _) in zip(times, times[1:]):
            assert end == start
        assert all(start <= end for start, end in times)

    def test_inner_boundaries_are_on_ass_resolution(self):
        times = sl.allocate_times(["あ" * 7, "い" * 13, "う" * 5], 0, 9999)
        for start, _ in times[1:]:
            assert start % 10 == 0

    def test_whitespace_is_not_counted(self):
        assert sl.allocate_times(["a b c d", "abcd"], 0, 8000) == [(0, 4000), (4000, 8000)]


# ---------------------------------------------------------------------------
# layout_turn (入口)
# ---------------------------------------------------------------------------
class TestLayoutTurn:
    def test_removes_citation_tags(self, measure):
        cues = sl.layout_turn("ですの。[src=4] 自分の側で動きますわね。[src=23]", 0, 6000, measure)
        assert "[src=" not in "".join(c.text for c in cues)

    def test_cues_stay_inside_the_utterance(self, measure):
        cues = sl.layout_turn(LONG_TURN, 2000, 20000, measure)
        assert cues[0].start_ms == 2000
        assert cues[-1].end_ms == 20000
        for a, b in zip(cues, cues[1:]):
            assert a.end_ms == b.start_ms

    def test_line_cap(self, measure):
        for cue in sl.layout_turn(LONG_TURN * 2, 0, 40000, measure):
            assert 1 <= len(cue.lines) <= sl.MAX_LINES

    def test_empty_text_gives_no_cue(self, measure):
        assert sl.layout_turn(" [src=1] ", 0, 3000, measure) == []

    def test_short_cue_is_merged_with_neighbour(self, measure):
        # 「はい。」単独だと 1.2 秒未満になるので隣と結合される (結合後 3 行までは許す)
        long_sentence = _grow_until("はい。", "そのあとに続く文は読点なしで長く", "続きますの。", 3, measure)
        cues = sl.layout_turn("はい。" + long_sentence, 0, 9000, measure)
        assert len(cues) == 1
        assert len(cues[0].lines) == 3

    def test_short_cue_is_kept_when_merge_would_exceed_cap(self, measure):
        # 隣と結合すると 4 行になるなら、短いまま出す
        # 空白のない英字の塊は 1 断片なので、3 行をちょうど使い切る長さを作れる
        n = next(n for n in range(1, 400) if len(sl.wrap_lines("a" * (n + 1) + "。", measure)) > 3)
        three_lines = "a" * n + "。"
        assert len(sl.wrap_lines(three_lines, measure)) == 3
        assert len(sl.wrap_lines("はい。" + three_lines, measure)) > sl.MAX_LINES
        cues = sl.layout_turn("はい。" + three_lines, 0, 20000, measure)
        assert cues[0].text == "はい。"
        assert cues[0].end_ms - cues[0].start_ms < sl.MIN_CUE_MS
        assert all(len(c.lines) <= sl.MAX_LINES for c in cues)

    def test_no_merge_when_cues_are_long_enough(self, measure):
        cues = sl.layout_turn(LONG_TURN, 0, 30000, measure)
        assert len(cues) >= 2
        assert all(c.end_ms - c.start_ms >= sl.MIN_CUE_MS for c in cues)
