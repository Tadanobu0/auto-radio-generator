"""サムネイル背景のプロンプトを「具体物＋題の余白」の形にするテスト (024)

- radio_director 080「寝る前のスマホ」・081「ヒートショック」で、自動プロンプトのサムネイル背景は
  9 枚全部が光の網・結晶・粒子のような抽象画になった
- 原因: 旧指示文 (THUMBNAIL_SYSTEM_PROMPT_TEMPLATE と user メッセージ) が ABSTRACT / METAPHORICAL・具体物なしを
  求めていた。さらに日本語の題・要約は _extract_english_keywords で英単語だけになり、
  "abstract concept, modern technology, innovative ideas" に置き換わっていた
- 新しい形: 構図 (正面・具体物は右 3 分の 1・左 3 分の 2 は無地の面・人と文字なし) はコードで固定し、
  LLM は場所・具体物・無地の面の 3 行だけを書く
- 旧メソッド generate_thumbnail_prompt と場面画像のプロンプトは変えない (既存テストのまま)
"""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from unittest.mock import patch

from core.interfaces.llm_port import LLMResponse
from core.models.usage import LLMUsage
from core.models.visual import VisualIdentity
from services.script_generation.image_prompt_generator import ImagePromptGenerator

FACTORY_PATH = "services.script_generation.image_prompt_generator.LLMAdapterFactory.create"

# 080 / 081 の台本の題と要約 (Mac の回収物はジョブから読めないので、ブリーフの記述から作った代わり)
THEME_080 = "寝る前のスマホ"
TITLE_080 = "寝る前のスマホ、何分前にやめればいい？"
SUMMARY_080 = "寝る直前のスマホが眠りに与える影響を、光と脳の覚醒の研究から整理する回。"
THEME_081 = "ヒートショック"
TITLE_081 = "冬の浴室で起きるヒートショックを防ぐには"
SUMMARY_081 = "冬の入浴時の急な温度差が体に与える負担と、家庭でできる対策を整理する回。"

# 080 / 081 で Mac 側が上書きして合格したプロンプトの要点を、LLM の 3 行の返答の形にしたもの
REPLY_080 = (
    "PLACE: a dim bedroom at night\n"
    "OBJECTS: a small wooden bedside table with one smartphone lying face down and a warm table lamp\n"
    "SURFACE: a bare dark indigo wall"
)
REPLY_081 = (
    "PLACE: a home bathroom on a winter night\n"
    "OBJECTS: a white bathtub with steam rising from the hot water and a wooden bath bucket beside it\n"
    "SURFACE: a bare plain tiled wall in pale grey"
)
# 080 / 081 の 1 回目に出た抽象画の型 (光の網・結晶・粒子)
ABSTRACT_REPLY = (
    "PLACE: an abstract dreamlike void\n"
    "OBJECTS: a glowing web of light with floating crystals and particles\n"
    "SURFACE: a soft color gradient"
)
ABSTRACT_WORDS = ["abstract", "metaphor", "particle", "crystal", "bokeh", "gradient", "energy", "aurora", "holographic"]


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        content=text,
        usage=LLMUsage(provider="ollama", model_name="m", input_tokens=1, output_tokens=1, request_count=1),
        finish_reason="stop",
    )


def _make_generator(mock_factory_create, *replies):
    port = Mock()
    port.generate = AsyncMock(side_effect=[_response(r) for r in replies])
    mock_factory_create.return_value = port
    config = Mock()
    config.yaml.script_generator.orchestrator.curator_model = "m"
    return ImagePromptGenerator(config), port


def _run(gen, theme=THEME_080, summary=SUMMARY_080, title=TITLE_080, identity=None):
    return asyncio.run(gen.generate_thumbnail_scene_prompt(theme, summary, title, identity))


def _no_abstract_words(prompt: str):
    lowered = prompt.lower()
    for word in ABSTRACT_WORDS:
        assert word not in lowered, word


# ---- 指示文 ----

@patch(FACTORY_PATH)
def test_llm_gets_japanese_theme_title_and_summary(mock_factory_create):
    """題・要約は日本語のまま渡す (英単語だけを抜くと題が消えていた)。"""
    gen, port = _make_generator(mock_factory_create, REPLY_080)
    _run(gen)
    request = port.generate.call_args.args[0]
    assert THEME_080 in request.user_prompt
    assert TITLE_080 in request.user_prompt
    assert SUMMARY_080 in request.user_prompt
    assert "abstract concept" not in request.user_prompt
    assert "ABSTRACT" not in request.user_prompt


@patch(FACTORY_PATH)
def test_system_prompt_asks_for_concrete_objects_not_abstract(mock_factory_create):
    gen, port = _make_generator(mock_factory_create, REPLY_080)
    _run(gen)
    system = port.generate.call_args.args[0].system_prompt
    assert system == ImagePromptGenerator.THUMBNAIL_SCENE_SYSTEM_PROMPT
    for phrase in ("realistic photograph", "straight on from the front", "right third", "left two thirds",
                   "concrete, everyday objects", "PLACE:", "OBJECTS:", "SURFACE:"):
        assert phrase in system, phrase
    # 抽象画は「求める」のではなく「避ける」側にだけ出てくる
    assert "ABSTRACT" not in system
    assert "never abstract art, light webs, crystals, particles, blurred light spots or a color gradient" in system
    assert "Never a person" in system


# ---- 組み立てたプロンプト (080 / 081) ----

@pytest.mark.parametrize("reply,theme,summary,title,place,objects,surface", [
    (REPLY_080, THEME_080, SUMMARY_080, TITLE_080, "a dim bedroom at night",
     "a small wooden bedside table with one smartphone lying face down and a warm table lamp",
     "a bare dark indigo wall"),
    (REPLY_081, THEME_081, SUMMARY_081, TITLE_081, "a home bathroom on a winter night",
     "a white bathtub with steam rising from the hot water and a wooden bath bucket beside it",
     "a bare plain tiled wall in pale grey"),
])
@patch(FACTORY_PATH)
def test_prompt_has_fixed_layout_around_llm_parts(mock_factory_create, reply, theme, summary, title,
                                                  place, objects, surface):
    gen, port = _make_generator(mock_factory_create, reply)
    prompt = _run(gen, theme, summary, title)
    assert port.generate.call_count == 1
    assert prompt.startswith(ImagePromptGenerator.SEGMENT_PROMPT_PREFIX)
    assert f"a realistic photograph of {place}, seen straight on from the front" in prompt
    assert f"In the right third of the frame, toward the right edge: {objects}, small" in prompt
    assert f"The left two thirds of the frame, including the center, is {surface}, completely flat" in prompt
    assert "kept clear for a title" in prompt
    assert prompt.endswith("shot on Kodak Portra 400 film, subtle film grain, highly detailed")
    assert len(prompt) <= ImagePromptGenerator.MAX_SEGMENT_PROMPT_CHARS
    _no_abstract_words(prompt)
    assert gen.warnings == []


@patch(FACTORY_PATH)
def test_visual_identity_colors_without_neon(mock_factory_create):
    gen, _ = _make_generator(mock_factory_create, REPLY_080)
    identity = VisualIdentity(
        primary_color="deep indigo", secondary_color="warm amber",
        color_mood="calm night", aesthetic="Cozy Lo-fi Studio", visual_keywords=["neon", "glow"],
    )
    prompt = _run(gen, identity=identity)
    assert "soft deep indigo and warm amber tones in the light, Cozy Lo-fi Studio aesthetic" in prompt
    # 色は面の照明の色合いとしてだけ使う (neon lighting・キーワードは入れない)
    assert "neon" not in prompt.lower()


# ---- 返答の検査とやり直し ----

@patch(FACTORY_PATH)
def test_abstract_reply_is_retried_with_a_note(mock_factory_create):
    gen, port = _make_generator(mock_factory_create, ABSTRACT_REPLY, REPLY_080)
    prompt = _run(gen)
    assert port.generate.call_count == 2
    retry_message = port.generate.call_args_list[1].args[0].user_prompt
    assert "CORRECTION:" in retry_message
    assert "abstract" in retry_message  # 何がだめだったかを伝える
    assert "smartphone lying face down" in prompt
    _no_abstract_words(prompt)
    assert gen.warnings == []


@patch(FACTORY_PATH)
def test_two_bad_replies_fall_back_to_a_concrete_scene(mock_factory_create):
    gen, port = _make_generator(mock_factory_create, ABSTRACT_REPLY, "A swirling cosmic aurora over the sea")
    prompt = _run(gen)
    assert port.generate.call_count == 2
    assert prompt == gen._get_fallback_thumbnail_scene_prompt()
    assert "In the right third of the frame" in prompt
    assert "a bare dark indigo wall" in prompt
    _no_abstract_words(prompt)
    assert len(gen.warnings) == 1
    assert "thumbnail background prompt fell back" in gen.warnings[0]
    assert "try 1: OBJECTS uses a word that is not allowed" in gen.warnings[0] or \
        "try 1: PLACE uses a word that is not allowed" in gen.warnings[0]
    assert "try 2: reply without PLACE / OBJECTS line" in gen.warnings[0]


@patch(FACTORY_PATH)
def test_llm_error_falls_back_to_the_concrete_scene_not_the_abstract_one(mock_factory_create):
    gen, port = _make_generator(mock_factory_create)
    port.generate = AsyncMock(side_effect=RuntimeError("proxy down"))
    prompt = _run(gen)
    assert prompt == gen._get_fallback_thumbnail_scene_prompt()
    assert not prompt.startswith("An abstract futuristic cityscape")
    _no_abstract_words(prompt)
    assert "LLM call failed (RuntimeError: proxy down)" in gen.warnings[0]


@pytest.mark.parametrize("objects", [
    "a woman sitting on the bed holding a phone",
    "a smartphone held in one hand",
    "a phone showing a face on its display",
    "a bottle with a large label",
    "a book with words on the cover",
    "a lamp casting a light web across the table",
])
def test_people_and_writing_words_are_rejected(objects):
    gen = ImagePromptGenerator.__new__(ImagePromptGenerator)
    parts, reason = gen._parse_thumbnail_parts(f"PLACE: a bedroom\nOBJECTS: {objects}\nSURFACE: a bare wall")
    assert parts is None
    assert reason.startswith("OBJECTS uses a word that is not allowed")


@pytest.mark.parametrize("objects", [
    "one smartphone lying face down on a bedside table",
    "a hollow wooden bucket with a sturdy handle",
    "a white towel with a textured weave on a plain matte unbranded shelf",
])
def test_ordinary_object_words_are_accepted(objects):
    gen = ImagePromptGenerator.__new__(ImagePromptGenerator)
    parts, reason = gen._parse_thumbnail_parts(f"PLACE: a bedroom\nOBJECTS: {objects}\nSURFACE: a bare wall")
    assert reason is None
    assert parts == ("a bedroom", objects, "a bare wall")


def test_markdown_and_quotes_are_stripped_and_surface_defaults():
    gen = ImagePromptGenerator.__new__(ImagePromptGenerator)
    parts, reason = gen._parse_thumbnail_parts(
        "Here you go:\n**PLACE:** \"a dim bedroom at night\".\n- OBJECTS: a small wooden bedside table,\n"
    )
    assert reason is None
    assert parts == ("a dim bedroom at night", "a small wooden bedside table", "a bare plain wall in a dark muted color")


def test_too_long_part_is_rejected():
    gen = ImagePromptGenerator.__new__(ImagePromptGenerator)
    long_objects = "a small wooden bedside table " + "and a ceramic cup " * 20
    parts, reason = gen._parse_thumbnail_parts(f"PLACE: a bedroom\nOBJECTS: {long_objects}")
    assert parts is None
    assert reason.startswith("OBJECTS too long")


def test_japanese_in_reply_is_removed():
    gen = ImagePromptGenerator.__new__(ImagePromptGenerator)
    parts, reason = gen._parse_thumbnail_parts("PLACE: a bathroom 浴室\nOBJECTS: a white bathtub 浴槽")
    assert reason is None
    assert parts[:2] == ("a bathroom", "a white bathtub")


def test_longest_parts_fit_the_prompt_limit():
    """固定部分は 1,000 字に対して、LLM の 3 行が 1 行 80 字ずつ入る余裕がある (超えたらやり直し)。"""
    gen = ImagePromptGenerator.__new__(ImagePromptGenerator)
    fixed = len(gen._compose_thumbnail_prompt("", "", ""))
    assert fixed + 3 * 80 <= ImagePromptGenerator.MAX_SEGMENT_PROMPT_CHARS


# ---- つなぎ (ThumbnailBackgroundGenerator) ----

def test_build_prompt_uses_the_scene_prompt():
    from services.media_processing.thumbnail_background_generator import ThumbnailBackgroundGenerator

    generator = ThumbnailBackgroundGenerator.__new__(ThumbnailBackgroundGenerator)
    generator.prompt_generator = Mock()
    generator.prompt_generator.generate_thumbnail_scene_prompt = AsyncMock(return_value="scene prompt")
    generator.prompt_generator.generate_thumbnail_prompt = AsyncMock(return_value="abstract prompt")
    prompt = asyncio.run(generator.build_prompt(THEME_081, SUMMARY_081, None, TITLE_081))
    assert prompt == "scene prompt"
    generator.prompt_generator.generate_thumbnail_scene_prompt.assert_awaited_once_with(
        theme=THEME_081, script_summary=SUMMARY_081, topic_title=TITLE_081, visual_identity=None,
    )
    generator.prompt_generator.generate_thumbnail_prompt.assert_not_awaited()


def test_scene_prompt_template_is_unchanged_by_024():
    """場面画像の指示文はそのまま (サムネイル用の語を入れていない)。"""
    assert "left two thirds" not in ImagePromptGenerator.SYSTEM_PROMPT_TEMPLATE
    assert "right third" not in ImagePromptGenerator.SYSTEM_PROMPT_TEMPLATE
