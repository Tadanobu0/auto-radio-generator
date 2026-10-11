"""
セグメント画像プロンプトの文字・人物抑制の回帰テスト

セグメント背景の不良 (手の破綻 / 画面・看板の疑似文字) はプロンプト生成の指示が原因だったため、
`ImagePromptGenerator` のセグメント用プロンプトを「人物なし・文字なし」に寄せた。本テストは:
  1. 抑制指示が生成プロンプトの先頭に置かれること
  2. 長さ上限を超えた出力は切り詰めずに固定文へフォールバックすること
  3. 固定文 (フォールバック) 自体が文字・人物を呼ばないこと
  4. システムプロンプト / 構図ガイドから文字・人物を呼ぶ描写要求が消えていること
  5. サムネイル用プロンプトには影響しないこと
を保護する。

非同期テストは test_image_prompt_generator.py と同じく sync 関数 + asyncio.run(...) で記述する。
"""

import asyncio
import re

import pytest
from unittest.mock import Mock, patch, AsyncMock

from core.interfaces.llm_port import LLMResponse
from core.models.curation import ScriptSegment
from core.models.usage import LLMUsage


CURATOR_MODEL_FOR_TEST = "deepseek-v4-flash"
FACTORY_PATH = "services.script_generation.image_prompt_generator.LLMAdapterFactory.create"

# 人物・文字を画面に呼び込む語。抑制文 (SEGMENT_PROMPT_PREFIX) を除いた部分に出てはならない。
PEOPLE_WORDS = ["person", "people", "hand", "finger", "face", "doctor", "engineer", "scientist", "patient"]
TEXT_WORDS = [
    "screen", "monitor", "display", "interface", "document", "chart", "graph",
    "diagram", "code", "sign", "label", "holographic", "data visualization",
]


def _make_port_mock(response_text: str) -> Mock:
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


def _make_segment(segment_type: str = "deep_dive") -> ScriptSegment:
    return ScriptSegment(
        segment_id=f"{segment_type}_1" if segment_type == "deep_dive" else segment_type,
        segment_type=segment_type,
        topic_title="音声合成の進化",
        turns=[
            {"speaker": "A", "text": "最新の音声合成はMOSスコア4.37に達しています"},
            {"speaker": "B", "text": "800時間の学習データが使われているんですね"},
        ],
    )


def _generate(mock_factory_create, response_text: str, segment_type: str = "deep_dive") -> str:
    from services.script_generation.image_prompt_generator import ImagePromptGenerator

    mock_factory_create.return_value = _make_port_mock(response_text)
    gen = ImagePromptGenerator(_make_mock_config())
    return asyncio.run(gen.generate_prompt(_make_segment(segment_type)))


def _contains_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}s?\b", text.lower()) is not None


class TestConstraintAtHead:
    """抑制指示は生成プロンプトの先頭に置かれる"""

    @patch(FACTORY_PATH)
    def test_prefix_is_prepended_to_llm_output(self, mock_factory_create):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        body = "A vintage condenser microphone on a brass stand in an empty recording booth"
        prompt = _generate(mock_factory_create, body)

        assert prompt == ImagePromptGenerator.SEGMENT_PROMPT_PREFIX + body

    def test_prefix_names_both_people_and_text(self):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        prefix = ImagePromptGenerator.SEGMENT_PROMPT_PREFIX.lower()
        for phrase in ["no people", "no hands", "no faces", "no text", "signage", "labels"]:
            assert phrase in prefix

    @patch(FACTORY_PATH)
    def test_wrapping_quotes_are_stripped(self, mock_factory_create):
        """LLM が全体を引用符で包んで返しても、引用符は画像プロンプトに残さない。"""
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        prompt = _generate(mock_factory_create, '"An empty server hall with cables overhead"')

        assert prompt == ImagePromptGenerator.SEGMENT_PROMPT_PREFIX + "An empty server hall with cables overhead"

    @patch(FACTORY_PATH)
    def test_no_trailing_no_text_suffix_is_appended(self, mock_factory_create):
        """抑制指示は先頭の1回だけ。旧実装の末尾 ", no text, no writing, no watermarks" は付けない。"""
        prompt = _generate(mock_factory_create, "An empty workshop at dawn")

        assert prompt.endswith("An empty workshop at dawn")
        assert prompt.lower().count("no text") == 1


class TestLengthLimit:
    """長さ上限: 超過時は切り詰めずに固定文へフォールバック"""

    @patch(FACTORY_PATH)
    def test_runaway_output_falls_back_instead_of_truncating(self, mock_factory_create):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        runaway = "A cluttered workbench with " + "capacitors resistors diodes " * 130  # 約3,600字
        prompt = _generate(mock_factory_create, runaway)

        gen = ImagePromptGenerator(_make_mock_config())
        assert prompt == gen._get_fallback_prompt(_make_segment())
        assert "capacitors" not in prompt

    @patch(FACTORY_PATH)
    def test_prompt_exactly_at_limit_is_kept(self, mock_factory_create):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        limit = ImagePromptGenerator.MAX_SEGMENT_PROMPT_CHARS
        body = "x" * (limit - len(ImagePromptGenerator.SEGMENT_PROMPT_PREFIX))
        prompt = _generate(mock_factory_create, body)

        assert len(prompt) == limit
        assert prompt.endswith(body)

    @patch(FACTORY_PATH)
    def test_prompt_one_char_over_limit_falls_back(self, mock_factory_create):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        limit = ImagePromptGenerator.MAX_SEGMENT_PROMPT_CHARS
        body = "x" * (limit - len(ImagePromptGenerator.SEGMENT_PROMPT_PREFIX) + 1)
        prompt = _generate(mock_factory_create, body)

        assert "xxxx" not in prompt
        assert len(prompt) <= limit

    def test_limit_cuts_observed_runaway_prompts(self):
        """調査で見つかった暴走プロンプト (1,398〜3,646字) がすべて上限を超えること。"""
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        assert ImagePromptGenerator.MAX_SEGMENT_PROMPT_CHARS < 1398

    @patch(FACTORY_PATH)
    def test_empty_output_falls_back(self, mock_factory_create):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        prompt = _generate(mock_factory_create, '  ""  ')

        gen = ImagePromptGenerator(_make_mock_config())
        assert prompt == gen._get_fallback_prompt(_make_segment())


class TestSegmentFallback:
    """固定文 (フォールバック) も文字・人物を呼ばない"""

    @pytest.mark.parametrize("segment_type", ["intro", "deep_dive", "conclusion"])
    @patch(FACTORY_PATH)
    def test_fallback_has_prefix_and_no_trigger_words(self, mock_factory_create, segment_type):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        gen = ImagePromptGenerator(_make_mock_config())
        prompt = gen._get_fallback_prompt(_make_segment(segment_type))

        prefix = ImagePromptGenerator.SEGMENT_PROMPT_PREFIX
        assert prompt.startswith(prefix)
        scene = prompt[len(prefix):]
        for word in PEOPLE_WORDS + TEXT_WORDS:
            assert not _contains_word(scene, word), f"fallback scene invites '{word}': {scene}"
        assert len(prompt) <= ImagePromptGenerator.MAX_SEGMENT_PROMPT_CHARS

    @patch(FACTORY_PATH)
    def test_api_error_returns_fallback_with_prefix(self, mock_factory_create):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        mock_port = Mock()
        mock_port.generate = AsyncMock(side_effect=RuntimeError("Mac Studio Proxy down"))
        mock_factory_create.return_value = mock_port

        gen = ImagePromptGenerator(_make_mock_config())
        prompt = asyncio.run(gen.generate_prompt(_make_segment("intro")))

        assert prompt.startswith(ImagePromptGenerator.SEGMENT_PROMPT_PREFIX)
        assert prompt == gen._get_fallback_prompt(_make_segment("intro"))


class TestSystemPromptPolicy:
    """システムプロンプトと構図ガイドが文字・人物を要求しない"""

    def test_system_prompt_forbids_people_and_text(self):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        template = ImagePromptGenerator.SYSTEM_PROMPT_TEMPLATE
        assert "NO people, hands, fingers, faces" in template
        assert "NO text, letters, numbers" in template
        assert "NO screens, monitors" in template
        assert "Imply human presence only through objects and light" in template

    def test_system_prompt_no_longer_requests_text_bearing_subjects(self):
        """旧プロンプトにあった、文字・人物を呼ぶ描写要求が残っていないこと。"""
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        template = ImagePromptGenerator.SYSTEM_PROMPT_TEMPLATE
        for old_phrase in [
            "What data, measurements, or visual information is referenced?",
            "What people, professions, or roles are mentioned?",
            "a smartphone app showing real-time blood sugar graphs",
            "A doctor in a white coat",
            "A software engineer's hands typing code",
            'ALWAYS end with: "no text, no writing, no watermarks"',
        ]:
            assert old_phrase not in template

    def test_system_prompt_examples_contain_no_trigger_words(self):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        template = ImagePromptGenerator.SYSTEM_PROMPT_TEMPLATE
        examples = template[template.index("EXAMPLE OUTPUTS"):]
        example_lines = [l for l in examples.splitlines() if l.startswith("A ")]
        assert len(example_lines) == 3
        for line in example_lines:
            for word in PEOPLE_WORDS + TEXT_WORDS:
                assert not _contains_word(line, word), f"example invites '{word}': {line}"

    @pytest.mark.parametrize("segment_type", ["intro", "deep_dive", "conclusion"])
    @patch(FACTORY_PATH)
    def test_composition_guidance_does_not_request_people_or_text(self, mock_factory_create, segment_type):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        gen = ImagePromptGenerator(_make_mock_config())
        guidance = gen._get_composition_guidance(segment_type)

        for phrase in ["hands", "people", "screens", "code", "statistics", "data visualizations", "patient"]:
            assert phrase not in guidance.lower(), f"guidance for {segment_type} invites '{phrase}'"

    @patch(FACTORY_PATH)
    def test_request_carries_new_system_prompt(self, mock_factory_create):
        """LLM に渡る system_prompt が新方針で、色・構図ガイドが展開されていること。"""
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        mock_port = _make_port_mock("An empty recording booth")
        mock_factory_create.return_value = mock_port
        gen = ImagePromptGenerator(_make_mock_config())
        asyncio.run(gen.generate_prompt(_make_segment("deep_dive")))

        request = mock_port.generate.call_args.args[0]
        assert "NO people, hands, fingers, faces" in request.system_prompt
        assert "{color_palette}" not in request.system_prompt
        assert "{composition_guidance}" not in request.system_prompt
        assert "Detailed investigation and analysis" in request.system_prompt
        assert "never copy names or figures from the script" in request.user_prompt


class TestThumbnailUnaffected:
    """サムネイル用プロンプトは今回の変更対象外"""

    @patch(FACTORY_PATH)
    def test_thumbnail_prompt_has_no_segment_prefix(self, mock_factory_create):
        from services.script_generation.image_prompt_generator import ImagePromptGenerator

        body = "An abstract flowing energy field with pulsing waves of light"
        mock_factory_create.return_value = _make_port_mock(body)
        gen = ImagePromptGenerator(_make_mock_config())
        prompt = asyncio.run(gen.generate_thumbnail_prompt(theme="AI", script_summary="summary"))

        assert prompt.startswith(body)
        assert ImagePromptGenerator.SEGMENT_PROMPT_PREFIX not in prompt
        assert prompt.endswith("no text, no writing, no watermarks")
