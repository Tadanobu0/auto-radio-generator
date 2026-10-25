"""画像プロンプトの固定文に「機器の表面は無地」を足し、draw にない記号を足させないことのテスト (015)

- radio_director-020 の Wi-Fi の回で、固定文があってもルーターの表面に崩れたロゴ風の文字が出た
- 同じ回の deep_dive_4_scene1 で、draw に無い "glowing emerald lines arcing" が足され、図解の記号のような光る扇が描かれた
- 背景の固定文だけを変え、サムネイルのプロンプトは今のまま
"""
import asyncio
from unittest.mock import AsyncMock, patch

from services.script_generation.image_prompt_generator import ImagePromptGenerator
from tests.test_scene_prompt_fallback import GOOD_BODY, SHORT_SENTENCE, _long_body, _response, _scene
from tests.test_scene_visuals import FACTORY_PATH, _make_generator

DEVICE_CLAUSE = "plain unbranded device surfaces with no logos, model names or printed characters"
NO_EXTRA_SYMBOLS = "Add no symbol or graphic that \"Draw\" does not name"


def test_prefix_has_plain_device_surface_clause():
    prefix = ImagePromptGenerator.SEGMENT_PROMPT_PREFIX
    assert DEVICE_CLAUSE in prefix
    # 既存の要素はそのまま、肯定形が先頭、最後は ": " で本文につなぐ
    assert prefix.startswith("An unpopulated, wordless scene with no people, no hands, no faces, no text, signage or labels, ")
    assert prefix.endswith(f"{DEVICE_CLAUSE}: ")
    assert prefix.lower().count("no text") == 1


@patch(FACTORY_PATH)
def test_scene_prompt_starts_with_device_clause(mock_factory_create):
    gen, _ = _make_generator(mock_factory_create, GOOD_BODY)
    prompt = asyncio.run(gen.generate_prompt(_scene()))
    assert prompt == gen.SEGMENT_PROMPT_PREFIX + GOOD_BODY
    assert DEVICE_CLAUSE in prompt
    assert len(prompt) <= gen.MAX_SEGMENT_PROMPT_CHARS


@patch(FACTORY_PATH)
def test_fallback_prompts_carry_device_clause(mock_factory_create):
    gen, port = _make_generator(mock_factory_create)
    # 汎用の文 (visuals の無い場面)
    for segment_type in ("intro", "deep_dive", "conclusion"):
        seg = _scene().model_copy(update={"segment_type": segment_type, "scene_visual": None})
        assert DEVICE_CLAUSE in gen._get_fallback_prompt(seg)
    # focus・draw からの短い英文
    port.generate = AsyncMock(side_effect=[
        _response(_long_body()), _response(_long_body(300)), _response(SHORT_SENTENCE),
    ])
    short = asyncio.run(gen.generate_prompt(_scene()))
    assert short.startswith(gen.SEGMENT_PROMPT_PREFIX + SHORT_SENTENCE.rstrip("."))
    assert len(short) <= gen.MAX_SEGMENT_PROMPT_CHARS


@patch(FACTORY_PATH)
def test_thumbnail_prompt_is_unchanged(mock_factory_create):
    body = "An abstract flowing aurora over a dark sea, shot on Kodak Portra 400 film, subtle film grain, highly detailed, no text"
    gen, _ = _make_generator(mock_factory_create, body)
    prompt = asyncio.run(gen.generate_thumbnail_prompt("Wi-Fi", "ルーターの置き場所の話"))
    assert prompt == body
    assert "unbranded" not in prompt
    assert "unbranded" not in gen._get_fallback_thumbnail_prompt("Wi-Fi")
    assert "unbranded" not in ImagePromptGenerator.THUMBNAIL_SYSTEM_PROMPT_TEMPLATE


@patch(FACTORY_PATH)
def test_scene_message_forbids_symbols_not_in_draw(mock_factory_create):
    gen, port = _make_generator(mock_factory_create, GOOD_BODY)
    asyncio.run(gen.generate_prompt(_scene()))
    user_prompt = port.generate.call_args.args[0].user_prompt
    instructions = user_prompt.split("INSTRUCTIONS:\n")[1].split("\n\n")[0].split("\n")
    # 番号の数は変えず、最後の手順に 1 文足す
    assert NO_EXTRA_SYMBOLS in instructions[-1]
    for symbol in ("glowing lines or arcs", "waves", "arrows", "Wi-Fi marks"):
        assert symbol in instructions[-1]


@patch(FACTORY_PATH)
def test_scene_message_without_avoid_also_forbids_symbols(mock_factory_create):
    gen, port = _make_generator(mock_factory_create, GOOD_BODY)
    asyncio.run(gen.generate_prompt(_scene(avoid=())))
    user_prompt = port.generate.call_args.args[0].user_prompt
    assert NO_EXTRA_SYMBOLS in user_prompt


@patch(FACTORY_PATH)
def test_short_scene_prompt_also_forbids_symbols(mock_factory_create):
    gen, port = _make_generator(mock_factory_create)
    port.generate = AsyncMock(side_effect=[
        _response(_long_body()), _response(_long_body(300)), _response(SHORT_SENTENCE),
    ])
    asyncio.run(gen.generate_prompt(_scene()))
    short_system = port.generate.call_args_list[2].args[0].system_prompt
    assert "Add no symbol or graphic that the direction does not name" in short_system
    assert "Wi-Fi marks" in short_system


@patch(FACTORY_PATH)
def test_scene_without_visuals_gets_no_symbol_line(mock_factory_create):
    """visuals の無い場面の指示文は変えない (draw が無いので)。"""
    gen, port = _make_generator(mock_factory_create, GOOD_BODY)
    asyncio.run(gen.generate_prompt(_scene().model_copy(update={"scene_visual": None})))
    user_prompt = port.generate.call_args.args[0].user_prompt
    assert NO_EXTRA_SYMBOLS not in user_prompt
