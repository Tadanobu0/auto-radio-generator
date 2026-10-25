"""
セグメント画像プロンプトの「絵と話のずれ」対策の回帰テスト (ブリーフ 007)

調査 006 で、絵と話のずれの主因は画像プロンプトの指示文にあると分かった。本テストは:
  1. 指示文の例 1 が寝室・医療機器でないこと、構図ガイドが健康の話題を医療機器に寄せないこと
  2. 小さな機器の表示部は「暗い表示窓 / 色の付いた光」だけ許すこと
  3. 主役の物・大きさ・材質・置き場所・画面に占める大きさ (4〜6 割) を必ず書かせること
  4. LLM に渡す本文が章の先頭だけでなく、各論点の最初のターン前後 (無ければ等分位置) から取られること
  5. 先頭の前置きが短く、物を呼び込む語 (screen) を含まないこと
を保護する。LLM はモック。
"""

import asyncio
import re
from pathlib import Path

import pytest
from unittest.mock import Mock, patch, AsyncMock

from core.interfaces.llm_port import LLMResponse
from core.models.curation import ScriptSegment
from core.models.usage import LLMUsage


CURATOR_MODEL_FOR_TEST = "deepseek-v4-flash"
FACTORY_PATH = "services.script_generation.image_prompt_generator.LLMAdapterFactory.create"
REAL_SCRIPT = Path(__file__).parent / "fixtures" / "real_scripts" / "20261006_llmglm_frontier.json"

# 旧プロンプトで例 1 にあった寝室・医療機器の語
OLD_EXAMPLE1_WORDS = ["bedside", "unmade bed", "insulin", "glucose", "sensor patch"]


def _make_port_mock(response_text: str = "A small brass desk fan on an oak shelf") -> Mock:
    port = Mock()
    port.generate = AsyncMock(
        return_value=LLMResponse(
            content=response_text,
            usage=LLMUsage(
                provider="ollama",
                model_name=CURATOR_MODEL_FOR_TEST,
                input_tokens=50,
                output_tokens=80,
                request_count=1,
            ),
            finish_reason="stop",
        )
    )
    return port


def _make_mock_config() -> Mock:
    config = Mock()
    config.yaml.script_generator.orchestrator.curator_model = CURATOR_MODEL_FOR_TEST
    return config


def _make_generator(mock_factory_create, response_text: str = "A small brass desk fan on an oak shelf"):
    from services.script_generation.image_prompt_generator import ImagePromptGenerator

    port = _make_port_mock(response_text)
    mock_factory_create.return_value = port
    return ImagePromptGenerator(_make_mock_config()), port


def _long_segment(key_claim_source_idxs=None, total_turns: int = 40) -> ScriptSegment:
    """40 ターン・1 ターン約 60 字の長い章。ターン 10 / 25 / 33 で初めて [src=7] / [src=8] / [src=9] を引く。"""
    turns = []
    for i in range(total_turns):
        text = f"ターン{i:02d}の話です。" + "これは本文のつなぎの文章でありとくに意味はありません。" * 2
        if i == 10:
            text = "ターン10で机の上の小型CO2モニターの話をしますの。[src=7]"
        elif i == 25:
            text = "ターン25では窓を少し開けて換気する話だよ。[src=8]"
        elif i == 33:
            text = "ターン33で棚の上の加湿器を紹介するのだ。[src=9, 3]"
        elif i in (26, 34):
            text += "[src=8]"  # 2 回目以降の引用は区切りにしない
        turns.append({"speaker": "A" if i % 2 == 0 else "B", "text": text})
    return ScriptSegment(
        segment_id="deep_dive_1",
        segment_type="deep_dive",
        topic_title="寝室の空気を測る",
        turns=turns,
        key_claim_source_idxs=key_claim_source_idxs or [],
    )


class TestExampleAndGuidance:
    """例 1 と構図ガイドが健康の話題を寝室・医療機器に引っ張らない"""

    def test_example1_is_not_bedroom_or_medical(self):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        template = ImagePromptGenerator.SYSTEM_PROMPT_TEMPLATE
        examples = template[template.index("EXAMPLE OUTPUTS"):]
        assert "Medical Topic" not in examples
        for word in OLD_EXAMPLE1_WORDS + ["bedroom", "hospital", "medical"]:
            assert word not in examples.lower(), f"example still contains '{word}'"

    def test_system_prompt_warns_against_defaulting_to_bedroom_or_hospital(self):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        template = ImagePromptGenerator.SYSTEM_PROMPT_TEMPLATE
        assert "Do NOT default to a bedroom, a hospital, or medical equipment" in template
        assert "never the objects or places" in template

    @pytest.mark.parametrize("segment_type", ["intro", "deep_dive", "conclusion"])
    @patch(FACTORY_PATH)
    def test_guidance_shows_topic_object_not_medical_equipment(self, mock_factory_create, segment_type):
        gen, _ = _make_generator(mock_factory_create)
        guidance = gen._get_composition_guidance(segment_type)

        assert "For medical topics" not in guidance
        for word in ["vials", "samples", "consultation room", "instruments laid out", "made bed"]:
            assert word not in guidance, f"{segment_type} guidance still steers to '{word}'"
        assert "For health and lifestyle topics: the" in guidance
        assert "40-60% of frame" in guidance


class TestSmallDeviceDisplay:
    """小さな機器の表示部は暗い窓 / 色の光だけ許す"""

    def test_dark_window_or_glow_allowed_without_digits(self):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        template = ImagePromptGenerator.SYSTEM_PROMPT_TEMPLATE
        assert "dark glass window or a plain colored glow" in template
        assert "never digits, letters, or icons" in template
        # 旧ルール「電源オフか背面・側面だけ」は外す (CO2 モニターが正体不明の箱になるため)
        assert "switched off or seen from behind/side" not in template


class TestHeroObjectSpecification:
    """主役の物・大きさ・材質・置き場所・画面に占める大きさを必ず書かせる"""

    def test_system_prompt_requires_five_attributes(self):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        template = ImagePromptGenerator.SYSTEM_PROMPT_TEMPLATE
        for label in ["- WHAT:", "- SIZE:", "- MATERIAL:", "- PLACE:", "- FRAME SHARE:"]:
            assert label in template
        assert "40 to 60 percent" in template
        assert "ALWAYS state the hero object's size, material, place, and frame share" in template

    def test_every_example_states_frame_share(self):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        template = ImagePromptGenerator.SYSTEM_PROMPT_TEMPLATE
        examples = template[template.index("EXAMPLE OUTPUTS"):]
        example_lines = [l for l in examples.splitlines() if l.startswith("A ")]
        assert len(example_lines) == 3
        for line in example_lines:
            assert "half of the frame" in line, f"example lacks frame share: {line}"

    @patch(FACTORY_PATH)
    def test_user_prompt_asks_for_size_material_place_and_share(self, mock_factory_create):
        gen, port = _make_generator(mock_factory_create)
        asyncio.run(gen.generate_prompt(_long_segment()))

        user_prompt = port.generate.call_args.args[0].user_prompt
        assert "state its size, material, where it is placed, and that it fills 40-60 percent of the frame" in user_prompt


class TestPrefix:
    """前置きは短く、物を呼び込む語を減らす"""

    def test_prefix_is_short_and_has_no_screen(self):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        prefix = ImagePromptGenerator.SEGMENT_PROMPT_PREFIX
        assert "screen" not in prefix.lower()
        assert "lettering" not in prefix.lower()
        # 015 で機器の表面の一文を足し、100 → 180 字に広げた
        assert len(prefix) <= 180
        # 肯定形の言い回しを先に置く
        assert prefix.startswith("An unpopulated, wordless scene")

    def test_system_prompt_tells_llm_not_to_name_excluded_things(self):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        assert "Do not name excluded things even to negate them" in ImagePromptGenerator.SYSTEM_PROMPT_TEMPLATE

    @patch(FACTORY_PATH)
    def test_user_prompt_uses_positive_wording(self, mock_factory_create):
        gen, port = _make_generator(mock_factory_create)
        asyncio.run(gen.generate_prompt(_long_segment()))

        user_prompt = port.generate.call_args.args[0].user_prompt
        assert "unpopulated and wordless" in user_prompt
        assert "screens" not in user_prompt


class TestSegmentExcerpts:
    """LLM に渡す本文: 章の先頭だけでなく章全体の要所から取る"""

    @patch(FACTORY_PATH)
    def test_short_segment_is_passed_whole(self, mock_factory_create):
        gen, _ = _make_generator(mock_factory_create)
        segment = ScriptSegment(
            segment_id="intro",
            segment_type="intro",
            topic_title="導入",
            turns=[
                {"speaker": "A", "text": "最初のターンです。[src=1]"},
                {"speaker": "B", "text": "二つ目のターンです。"},
            ],
        )
        excerpt = gen._select_dialogue_excerpts(segment)

        assert excerpt == "最初のターンです。 二つ目のターンです。"

    @patch(FACTORY_PATH)
    def test_key_claim_anchors_pick_first_citing_turn_and_neighbours(self, mock_factory_create):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        gen, _ = _make_generator(mock_factory_create)
        segment = _long_segment(key_claim_source_idxs=[7, 8, 9])
        excerpt = gen._select_dialogue_excerpts(segment)

        assert len(excerpt) <= ImagePromptGenerator.MAX_CONTEXT_CHARS
        # 各論点の最初のターンとその前後
        for i in (9, 10, 11, 24, 25, 26, 32, 33, 34):
            assert f"ターン{i:02d}" in excerpt, f"turn {i} missing from excerpt"
        # 先頭付近・論点から離れたターンは入らない
        for i in (0, 5, 18, 39):
            assert f"ターン{i:02d}" not in excerpt
        # 引用タグは渡さない
        assert "[src=" not in excerpt
        assert excerpt.count("\n") == 2

    @patch(FACTORY_PATH)
    def test_key_claim_anchor_ignores_unknown_source(self, mock_factory_create):
        gen, _ = _make_generator(mock_factory_create)
        anchors = gen._key_claim_anchor_turns(_long_segment().turns, [99, 9, 7])

        assert anchors == [10, 33]

    @patch(FACTORY_PATH)
    def test_without_key_claims_excerpts_are_evenly_spaced(self, mock_factory_create):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        gen, _ = _make_generator(mock_factory_create)
        excerpt = gen._select_dialogue_excerpts(_long_segment())

        assert len(excerpt) <= ImagePromptGenerator.MAX_CONTEXT_CHARS
        # 40 ターンを 4 等分: 0 / 10 / 20 / 30 の前後
        for i in (0, 1, 9, 10, 11, 19, 20, 21, 29, 30, 31):
            assert f"ターン{i:02d}" in excerpt, f"turn {i} missing from excerpt"
        assert "ターン39" not in excerpt
        assert excerpt.count("\n") == 3

    @patch(FACTORY_PATH)
    def test_no_anchor_found_falls_back_to_even_spacing(self, mock_factory_create):
        gen, _ = _make_generator(mock_factory_create)
        with_unknown = gen._select_dialogue_excerpts(_long_segment(key_claim_source_idxs=[99]))
        without = gen._select_dialogue_excerpts(_long_segment())

        assert with_unknown == without

    @patch(FACTORY_PATH)
    def test_overlapping_windows_do_not_repeat_turns(self, mock_factory_create):
        gen, _ = _make_generator(mock_factory_create)
        segment = _long_segment()
        segment.turns[11]["text"] += "[src=5]"
        segment.key_claim_source_idxs = [7, 5]  # 隣り合う論点ターン 10 と 11
        excerpt = gen._select_dialogue_excerpts(segment)

        assert excerpt.split("\n")[0].startswith("ターン09")
        assert excerpt.split("\n")[1].startswith("ターン11")
        for i in (9, 10, 11, 12):
            assert excerpt.count(f"ターン{i:02d}") == 1

    @patch(FACTORY_PATH)
    def test_clustered_key_claims_each_keep_their_turn(self, mock_factory_create):
        """論点が章の前半に固まっていても、各論点のターンが抜粋から落ちない。"""
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        gen, _ = _make_generator(mock_factory_create)
        segment = _long_segment()
        for i, src in zip(range(1, 8), range(101, 108)):
            segment.turns[i]["text"] += f"[src={src}]"
        segment.turns[36]["text"] = "ターン36で最後の論点の話をします。[src=200]"
        segment.key_claim_source_idxs = list(range(101, 108)) + [200]
        excerpt = gen._select_dialogue_excerpts(segment)

        assert len(excerpt) <= ImagePromptGenerator.MAX_CONTEXT_CHARS
        for i in list(range(1, 8)) + [36]:
            assert f"ターン{i:02d}" in excerpt, f"key-claim turn {i} missing from excerpt"

    @patch(FACTORY_PATH)
    def test_long_excerpt_is_cut_within_budget(self, mock_factory_create):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        gen, _ = _make_generator(mock_factory_create)
        segment = _long_segment()
        for turn in segment.turns:
            turn["text"] = "あ" * 500  # 句点の無い長いターン
        excerpt = gen._select_dialogue_excerpts(segment)

        assert len(excerpt) <= ImagePromptGenerator.MAX_CONTEXT_CHARS
        assert all(part.endswith("…") for part in excerpt.split("\n"))

    @patch(FACTORY_PATH)
    def test_llm_receives_late_key_point(self, mock_factory_create):
        """旧実装 (先頭 800 字) では届かなかった章後半の論点が LLM に渡る。"""
        gen, port = _make_generator(mock_factory_create)
        asyncio.run(gen.generate_prompt(_long_segment(key_claim_source_idxs=[7, 8, 9])))

        user_prompt = port.generate.call_args.args[0].user_prompt
        assert "棚の上の加湿器" in user_prompt
        assert "Topic: 寝室の空気を測る" in user_prompt


class TestLoaderCarriesKeyClaims:
    """外部台本の key_claims の出典番号が ScriptSegment まで運ばれる"""

    def test_deep_dive_gets_source_idxs_in_order(self):
        from core.models.verified_script import VerifiedScript
        from services.script_loading.radio_director_loader import build_script_segments

        vs = VerifiedScript.model_validate_json(REAL_SCRIPT.read_text(encoding="utf-8"))
        segments = build_script_segments(vs)
        by_id = {s.segment_id: s for s in segments}

        assert by_id["intro"].key_claim_source_idxs == []
        assert by_id["conclusion"].key_claim_source_idxs == []
        assert by_id["deep_dive_1"].key_claim_source_idxs == [22, 19, 25, 21, 39, 36, 35]
        # 重複 (src=14 が 2 回) は 1 つにまとめる
        assert by_id["deep_dive_2"].key_claim_source_idxs == [11, 14, 20, 4]

    def test_real_script_excerpts_reach_later_key_points(self):
        """実台本: deep_dive_1 の抜粋に、先頭 800 字では届かない後半の論点 (src=36 / 35) のターンが入る。"""
        from core.models.verified_script import VerifiedScript
        from services.script_generation.image_prompt_generator import ImagePromptGenerator
        from services.script_loading.radio_director_loader import build_script_segments

        vs = VerifiedScript.model_validate_json(REAL_SCRIPT.read_text(encoding="utf-8"))
        segment = {s.segment_id: s for s in build_script_segments(vs)}["deep_dive_1"]

        with patch(FACTORY_PATH) as mock_factory_create:
            gen, _ = _make_generator(mock_factory_create)
        excerpt = gen._select_dialogue_excerpts(segment)

        assert len(excerpt) <= ImagePromptGenerator.MAX_CONTEXT_CHARS
        assert "GLM-Z1-9B" in excerpt
        assert not re.search(r"\[src=", excerpt)
