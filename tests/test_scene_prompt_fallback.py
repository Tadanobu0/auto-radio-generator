"""visuals からの画像プロンプト作りの取りこぼしを直すことのテスト (014)

- プロンプト生成が失敗して汎用の文・既定の文に替えたときは、章・場面・理由を warnings に入れる
  (ImagePromptGenerator → ImageProvider → RenderResult → WorkflowResult)
- scene_visual がある場面は、汎用の文に落とす前に 1 回作り直す。だめなら focus・draw からの短い英文にする
- draw の高さ・床から離す語が、プロンプトで low / floor や機器の大きさの言葉になっていないかを機械的に確かめ、
  なっていたら作り直す
- 試験用データは radio_director-019 の写しと同じ形 (深掘り 3 章が 3・4・4 場面、avoid はリスト) に、
  019 で問題が出た 2 場面の draw を入れたもの
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.interfaces.llm_port import LLMResponse
from core.models.curation import SceneVisual, ScriptSegment
from core.models.generation_metadata import GenerationMetadata
from core.models.usage import LLMUsage
from services.media_processing.image_provider import ImageProvider
from services.script_generation.scene_placement_check import (
    draw_height_parts,
    find_placement_problem,
    wants_off_floor,
)
from tests.test_scene_visuals import FACTORY_PATH, _deep_dive_indices, _make_generator, _run_timeline
from tests.test_scene_visuals_avoid_list import _load, _wifi_raw

# 019 で問題が出た 2 場面の draw (ブリーフの写し)
DD2_SCENE1_DRAW = "アンテナが2本ある黒いルーター1台の寄りの絵。リビングの腰の高さの木の棚の上に置かれ、背景に奥へ続く廊下"
DD3_SCENE3_DRAW = "部屋の中央寄りの腰の高さの棚の上の白いルーター1台"
# 019 で実際に出た、置き場所が崩れたプロンプトの一部
BAD_019_BODY = (
    "one waist-high white router on a low shelf near the center of the room, matte plastic casing, "
    "filling about half of the frame, soft teal glow, shot on Kodak Portra 400 film"
)
GOOD_BODY = (
    "One palm-sized white router standing on a waist-height wooden shelf toward the middle of the room, "
    "matte plastic casing, filling about half of the frame, soft teal glow, shot on Kodak Portra 400 film"
)
GENERIC_DEEP_DIVE = "A quiet workshop bench with precision tools and metal components laid out in neat rows"
SHORT_SENTENCE = "One black router with two antennas on a waist-height wooden shelf in a living room, a hallway receding behind it."


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        content=text,
        usage=LLMUsage(provider="ollama", model_name="m", input_tokens=1, output_tokens=1, request_count=1),
        finish_reason="stop",
    )


def _long_body(words: int = 220) -> str:
    return "A black router with two antennas on a waist-height wooden shelf, " + ", ".join(
        f"detail {k}" for k in range(words)
    )


def _scene(segment_id="deep_dive_2_scene1", draw=DD2_SCENE1_DRAW, avoid=("ルーターが床に直置きされている",)):
    return ScriptSegment(
        segment_id=segment_id,
        segment_type="deep_dive",
        topic_title="ルーターの置き場所",
        turns=[{"speaker": "A", "text": "ルーターは腰の高さの棚に置くのがいいのだ。[src=2]"}],
        scene_visual=SceneVisual(start_turn=0, focus="置き場所の高さ", draw=draw, avoid=list(avoid)),
    )


def _wifi_019_raw() -> dict:
    """019 の写しの形に、問題が出た 2 場面の draw を入れたもの。"""
    raw = _wifi_raw()
    dd = _deep_dive_indices(raw)
    raw["script"]["segments"][dd[1]]["visuals"][0]["draw"] = DD2_SCENE1_DRAW
    raw["script"]["segments"][dd[2]]["visuals"][2]["draw"] = DD3_SCENE3_DRAW
    return raw


# ---------------------------------------------------------------- 置き場所の機械チェック

def test_019_dd3_scene3_prompt_is_flagged():
    problem = find_placement_problem(DD3_SCENE3_DRAW, [], BAD_019_BODY)
    assert problem is not None and "low shelf" in problem


def test_height_word_used_as_object_size_is_flagged():
    body = "One waist-high white router beside a window, matte plastic"
    problem = find_placement_problem(DD3_SCENE3_DRAW, [], body)
    assert problem is not None and "waist-high" in problem


@pytest.mark.parametrize("body", [
    GOOD_BODY,
    "A white router at waist height on a wooden shelf in the middle of the room",
    "A white router on a waist-high oak cabinet",
    "Two small white routers on a wooden shelf",          # 高さを書かないだけなら通す
    "A white router on a shelf, the hallway floor receding into darkness behind it",  # 背景の床は置き場所ではない
])
def test_placement_kept_passes(body):
    assert find_placement_problem(DD3_SCENE3_DRAW, [], body) is None


@pytest.mark.parametrize("body", [
    "A white router resting on the floor beside a sofa",
    "A white router on the wooden floor near a wall",
    "A white router on a low wooden table",
    "A white router at floor level",
])
def test_floor_or_low_place_is_flagged_when_draw_is_raised(body):
    assert find_placement_problem(DD2_SCENE1_DRAW, [], body) is not None


def test_avoid_on_floor_also_triggers_floor_check():
    draw = "棚の上の白いルーター1台"  # 高さの語は無いが、avoid が床置きを避ける
    assert wants_off_floor(draw, ["ルーターが床に直置きされている"])
    assert find_placement_problem(draw, ["ルーターが床に直置きされている"], "A router on the floor") is not None
    assert find_placement_problem(draw, [], "A router on a low table") is None


def test_draw_that_puts_something_on_the_floor_is_not_checked():
    draw = "床に置いた加湿器1台と、腰の高さの棚"
    assert not wants_off_floor(draw, [])
    assert find_placement_problem(draw, [], "A humidifier on the floor next to a waist-height shelf") is None


def test_height_parts():
    assert draw_height_parts(DD2_SCENE1_DRAW) == ["waist"]
    assert draw_height_parts("目の高さの壁掛けの時計") == ["eye"]
    assert draw_height_parts("机の上のマグカップ") == []


# ---------------------------------------------------------------- 指示を強める

@patch(FACTORY_PATH)
def test_scene_message_asks_to_keep_placement_words(mock_factory_create):
    gen, port = _make_generator(mock_factory_create, GOOD_BODY)
    asyncio.run(gen.generate_prompt(_scene(draw=DD3_SCENE3_DRAW)))
    user_prompt = port.generate.call_args.args[0].user_prompt
    assert "on a waist-height shelf" in user_prompt
    assert "never how big it is" in user_prompt
    assert f"- Draw: {DD3_SCENE3_DRAW}" in user_prompt
    numbers = [line.split(".")[0] for line in user_prompt.split("INSTRUCTIONS:\n")[1].split("\n\n")[0].split("\n")]
    assert numbers == [str(k) for k in range(1, len(numbers) + 1)]
    assert port.generate.await_count == 1
    assert gen.warnings == []


# ---------------------------------------------------------------- 作り直しと、汎用の文に落とさないこと

@patch(FACTORY_PATH)
def test_placement_failure_is_retried_with_correction(mock_factory_create):
    gen, port = _make_generator(mock_factory_create)
    port.generate = AsyncMock(side_effect=[_response(BAD_019_BODY), _response(GOOD_BODY)])
    prompt = asyncio.run(gen.generate_prompt(_scene("deep_dive_3_scene3", DD3_SCENE3_DRAW, avoid=())))
    assert prompt == gen.SEGMENT_PROMPT_PREFIX + GOOD_BODY
    assert port.generate.await_count == 2
    retry = port.generate.call_args_list[1].args[0].user_prompt
    assert "CORRECTION:" in retry and "low shelf" in retry and "on a waist-height" in retry
    assert gen.warnings == []  # 作り直しで直れば、汎用・既定の文に替えていない


@patch(FACTORY_PATH)
def test_too_long_output_is_retried(mock_factory_create):
    gen, port = _make_generator(mock_factory_create)
    port.generate = AsyncMock(side_effect=[_response(_long_body()), _response(GOOD_BODY)])
    prompt = asyncio.run(gen.generate_prompt(_scene()))
    assert prompt == gen.SEGMENT_PROMPT_PREFIX + GOOD_BODY
    assert "60-110 words" in port.generate.call_args_list[1].args[0].user_prompt
    assert gen.warnings == []


@patch(FACTORY_PATH)
def test_two_failures_fall_back_to_short_prompt_from_draw(mock_factory_create):
    """019 の deep_dive_2_scene1: 汎用の文 (workshop bench) にせず、focus・draw の短い英文にする。"""
    gen, port = _make_generator(mock_factory_create)
    port.generate = AsyncMock(side_effect=[
        _response(_long_body()), _response(_long_body(300)), _response(SHORT_SENTENCE),
    ])
    prompt = asyncio.run(gen.generate_prompt(_scene()))
    assert GENERIC_DEEP_DIVE not in prompt
    assert prompt.startswith(gen.SEGMENT_PROMPT_PREFIX + SHORT_SENTENCE.rstrip("."))
    assert gen.DEFAULT_COLOR_PALETTE in prompt and "Kodak Portra 400" in prompt
    assert len(prompt) <= gen.MAX_SEGMENT_PROMPT_CHARS

    short_request = port.generate.call_args_list[2].args[0]
    assert f"Draw: {DD2_SCENE1_DRAW}" in short_request.user_prompt
    assert "Point of this scene: 置き場所の高さ" in short_request.user_prompt
    assert "waist-height shelf" in short_request.system_prompt

    assert len(gen.warnings) == 1
    warning = gen.warnings[0]
    assert warning.startswith("image prompt for deep_dive_2 scene 1 (deep_dive_2_scene1) fell back:")
    assert "try 1: LLM output too long" in warning and "try 2: LLM output too long" in warning
    assert warning.endswith("used a short prompt built from focus/draw")


@patch(FACTORY_PATH)
def test_two_placement_failures_fall_back_to_short_prompt(mock_factory_create):
    gen, port = _make_generator(mock_factory_create)
    port.generate = AsyncMock(side_effect=[
        _response(BAD_019_BODY), _response(BAD_019_BODY), _response("One white router on a waist-height shelf."),
    ])
    prompt = asyncio.run(gen.generate_prompt(_scene("deep_dive_3_scene3", DD3_SCENE3_DRAW, avoid=())))
    assert "low shelf" not in prompt
    assert "deep_dive_3 scene 3 (deep_dive_3_scene3)" in gen.warnings[0]
    assert "placement check failed" in gen.warnings[0]


@patch(FACTORY_PATH)
def test_llm_errors_are_retried_then_reported(mock_factory_create):
    gen, port = _make_generator(mock_factory_create)
    port.generate = AsyncMock(side_effect=[
        RuntimeError("Mac Studio Proxy down"), _response(GOOD_BODY),
    ])
    assert asyncio.run(gen.generate_prompt(_scene())) == gen.SEGMENT_PROMPT_PREFIX + GOOD_BODY
    assert gen.warnings == []


@patch(FACTORY_PATH)
def test_llm_down_uses_generic_prompt_with_warning(mock_factory_create):
    gen, port = _make_generator(mock_factory_create)
    port.generate = AsyncMock(side_effect=RuntimeError("Mac Studio Proxy down"))
    prompt = asyncio.run(gen.generate_prompt(_scene()))
    assert prompt == gen._get_fallback_prompt(_scene())
    assert port.generate.await_count == 3
    warning = gen.warnings[0]
    assert "deep_dive_2 scene 1" in warning and "RuntimeError: Mac Studio Proxy down" in warning
    assert warning.endswith("used the generic fallback prompt (no usable LLM output)")


@patch(FACTORY_PATH)
def test_short_prompt_failure_uses_rejected_prompt_not_generic(mock_factory_create):
    gen, port = _make_generator(mock_factory_create)
    long_body = _long_body()
    port.generate = AsyncMock(side_effect=[
        _response(long_body), _response(BAD_019_BODY), RuntimeError("timeout"),
    ])
    prompt = asyncio.run(gen.generate_prompt(_scene("deep_dive_3_scene3", DD3_SCENE3_DRAW, avoid=())))
    # 置き場所だけ崩れたものを、over-long を切ったものより先に使う
    assert prompt == gen.SEGMENT_PROMPT_PREFIX + BAD_019_BODY
    assert gen.warnings[0].endswith("used the rejected prompt as is")

    port.generate = AsyncMock(side_effect=[_response(long_body), _response(long_body), RuntimeError("timeout")])
    gen.warnings.clear()
    prompt = asyncio.run(gen.generate_prompt(_scene()))
    assert GENERIC_DEEP_DIVE not in prompt
    assert prompt.startswith(gen.SEGMENT_PROMPT_PREFIX + "A black router with two antennas")
    assert len(prompt) <= gen.MAX_SEGMENT_PROMPT_CHARS
    assert gen.warnings[0].endswith("used the over-long prompt cut at a comma")


@patch(FACTORY_PATH)
def test_scene_without_visual_still_uses_generic_prompt_but_warns(mock_factory_create):
    gen, port = _make_generator(mock_factory_create)
    port.generate = AsyncMock(return_value=_response(_long_body()))
    seg = _scene().model_copy(update={"segment_id": "deep_dive_1", "scene_visual": None})
    prompt = asyncio.run(gen.generate_prompt(seg))
    assert prompt == gen._get_fallback_prompt(seg)
    assert port.generate.await_count == 1  # visuals の無い場面は作り直さない (従来どおり)
    assert gen.warnings == [
        f"image prompt for deep_dive_1 scene 1 (deep_dive_1) fell back: LLM output too long "
        f"({len(gen.SEGMENT_PROMPT_PREFIX + _long_body())} chars > 1000); used the generic fallback prompt"
    ]


# ---------------------------------------------------------------- 019 の写しで、warnings が Mac に返る形まで

def _provider_with_generator(tmp_path, monkeypatch, gen):
    monkeypatch.chdir(tmp_path)
    config = SimpleNamespace(yaml=SimpleNamespace(video_renderer=SimpleNamespace(background_mode="static")))
    provider = ImageProvider(config, output_dir=tmp_path / "run")
    provider.mode = "dynamic"
    provider.prompt_generator = gen

    async def generate_image(prompt, output_path, **kwargs):
        output_path.write_bytes(b"png")
        return output_path, GenerationMetadata.create_from_segment(
            image_path=str(output_path), segment_id=kwargs["segment_id"], segment_type=kwargs["segment_type"],
            prompt=prompt, visual_identity={}, seed=7, generation_time_sec=1.0,
            resolution="1024x576", steps=4, sampler="euler", cfg_scale=1.0,
        )

    provider.image_client = SimpleNamespace(generate_image=generate_image)
    return provider


@patch(FACTORY_PATH)
def test_wifi_019_copy_reports_only_the_failed_scenes(mock_factory_create, mock_app_config, tmp_path, monkeypatch):
    """019 の写しを場面に切り、dd2 scene1 は 2 回とも長すぎ、dd3 scene3 は 2 回とも置き場所が崩れる LLM で流す。"""
    segments = _load(_wifi_019_raw())
    _, asked, _ = _run_timeline(mock_app_config, segments)
    scenes = [s for s in asked if s.scene_visual is not None]
    assert len(scenes) == 11

    gen, port = _make_generator(mock_factory_create)
    counter = {"n": 0}

    async def fake_generate(request):
        counter["n"] += 1
        user = request.user_prompt
        if user.startswith("Point of this scene:"):  # focus・draw からの短い英文
            return _response("One router on a waist-height wooden shelf.")
        if DD2_SCENE1_DRAW in user:
            return _response(_long_body())
        if DD3_SCENE3_DRAW in user:
            return _response(BAD_019_BODY)
        return _response(f"One white router on a wooden shelf, variant {counter['n']}")

    port.generate = fake_generate
    provider = _provider_with_generator(tmp_path, monkeypatch, gen)
    for scene in scenes:
        asyncio.run(provider.get_image_for_segment(scene))

    assert len(provider.warnings) == 2
    assert provider.warnings[0].startswith("image prompt for deep_dive_2 scene 1 (deep_dive_2_scene1) fell back:")
    assert "too long" in provider.warnings[0]
    assert provider.warnings[1].startswith("image prompt for deep_dive_3 scene 3 (deep_dive_3_scene3) fell back:")
    assert "placement check failed" in provider.warnings[1]
    assert gen.warnings == []  # provider に移した

    records = [
        json.loads(line)
        for line in (tmp_path / "run" / "generation_metadata.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    by_id = {r["segment_id"]: r["prompt"] for r in records}
    assert GENERIC_DEEP_DIVE not in by_id["deep_dive_2_scene1"]
    assert "waist-height wooden shelf" in by_id["deep_dive_2_scene1"]
    assert "low shelf" not in by_id["deep_dive_3_scene3"]


def test_static_image_fallback_is_reported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = SimpleNamespace(yaml=SimpleNamespace(video_renderer=SimpleNamespace(background_mode="static")))
    provider = ImageProvider(config, output_dir=tmp_path / "run")
    provider.mode = "dynamic"
    provider.prompt_generator = SimpleNamespace(generate_prompt=AsyncMock(return_value="A router"), warnings=[])

    async def broken(prompt, output_path, **kwargs):
        raise RuntimeError("ComfyUI is not running")

    provider.image_client = SimpleNamespace(generate_image=broken)
    provider._select_static_image = lambda segment: Path("assets/backgrounds/deep_dive_1.png")
    asyncio.run(provider.get_image_for_segment(_scene()))
    assert provider.warnings == [
        "background image for deep_dive_2_scene1: image generation failed "
        "(RuntimeError: ComfyUI is not running); used a static image"
    ]


def test_render_result_warnings_include_image_warnings(mock_app_config):
    from services.video_rendering import ffmpeg_renderer as fr

    renderer = fr.FfmpegRenderer.__new__(fr.FfmpegRenderer)
    renderer.timeline_calculator = SimpleNamespace(warnings=["visuals of deep_dive_1 ignored: x"])
    provider = SimpleNamespace(warnings=["image prompt for deep_dive_2 scene 1 (deep_dive_2_scene1) fell back: y"])
    assert renderer._collect_warnings(provider) == [
        "visuals of deep_dive_1 ignored: x",
        "image prompt for deep_dive_2 scene 1 (deep_dive_2_scene1) fell back: y",
    ]
    assert renderer._collect_warnings(SimpleNamespace()) == ["visuals of deep_dive_1 ignored: x"]
