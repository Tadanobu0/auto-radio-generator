"""画像だけを先に作る実行・場面の作り直し・画像の使い回しと、置き場所の検査の誤判定の修正 (016)

A. 加湿器の回 (radio_director-022) で、draw の「目の高さから見た絵」とプロンプトの eye-level view を
   物の高さの語とみなして 17 場面中 5 場面を短いプロンプトに落とした。視点の言い方は高さとみなさない。
   短いプロンプトには avoid の逆を肯定形で、draw の高さ・距離の語とあわせてコードで足す。
B. run_workflow_sync(images_only=True) は背景画像までで止まる。regen_scenes は指定の場面だけ
   キャッシュを使わず seed をずらす。reuse_images_from は前の run の画像をそのまま使う。
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import services.media_processing
import workflow
from core.models.curation import SceneVisual, ScriptSegment
from core.models.generation_metadata import GenerationMetadata
from services.media_processing.image_provider import ImageProvider
from services.media_processing.scene_images import (
    SCENE_IMAGES_DIR,
    SCENE_IMAGES_MANIFEST,
    base_seed_for,
    build_scene_image_options,
    estimate_turn_times,
    export_scene_images,
    scene_index_of,
)
from services.script_generation.scene_placement_check import (
    draw_height_parts,
    find_placement_problem,
    strip_draw_viewpoint,
    wants_off_floor,
)
from services.script_generation.scene_placement_phrases import (
    avoid_opposite_phrases,
    draw_placement_phrases,
    short_prompt_placement_phrases,
)
from tests.test_scene_prompt_fallback import _long_body, _response
from tests.test_scene_visuals import FACTORY_PATH, _make_generator
from tests.test_workflow_no_upload import _FakeYouTubeClient, _run, stubbed_workflow  # noqa: F401

# ---------------------------------------------------------------- A. 加湿器の回の 5 場面

# 台本の構図の規則で「目の高さから見た」が入った draw と、それを英語にしたプロンプト (draw, avoid, prompt)
HUMIDIFIER_SCENES = [
    (
        "目の高さから見た正面のリビングの絵。テレビから離れた部屋の中央寄りの腰の高さの台の上に白い加湿器1台",
        ["テレビの真下に加湿器がある"],
        "An eye-level view of a calm living room, one palm-sized white humidifier standing on a "
        "waist-height wooden stand toward the middle of the room, filling about half of the frame",
    ),
    (
        "目の高さから見た子ども部屋の絵。手の届かない高い棚の上の加湿器1台",
        ["子どもの手が届く低い台に置かれている"],
        "A children's room seen from eye level, one small humidifier up on a high wall shelf, "
        "matte white casing, filling about half of the frame",
    ),
    (
        "目線の高さから見た寝室の絵。ベッドから2m離れた胸の高さのチェストの上の加湿器1台",
        ["枕元に加湿器がある"],
        "Viewed from eye height, a quiet bedroom with one humidifier on a chest-height chest of drawers "
        "across the room, soft morning light",
    ),
    (
        "目の高さから見た窓のある部屋の絵。部屋の中央寄りの腰の高さの棚の上の加湿器1台",
        ["窓際に置かれている"],
        "Eye-level frontal view of a room, one humidifier on a waist-height shelf toward the middle "
        "of the room, filling about half of the frame",
    ),
    (
        "目の高さから見た部屋の絵。壁から30cm離した腰の高さの台の上の加湿器1台",
        ["壁際に置かれている", "床に直置きされている"],
        "An eye-height camera looks at one humidifier on a waist-height stand a little away from the wall, "
        "shot at eye level, filling about half of the frame",
    ),
]


@pytest.mark.parametrize("draw,avoid,prompt", HUMIDIFIER_SCENES)
def test_humidifier_viewpoint_words_are_not_heights(draw, avoid, prompt):
    assert find_placement_problem(draw, avoid, prompt) is None


def test_viewpoint_is_removed_from_draw_but_place_heights_stay():
    assert draw_height_parts("目の高さから見た正面の部屋の絵") == []
    assert draw_height_parts("目線の高さから見た部屋。腰の高さの棚の上のルーター") == ["waist"]
    assert draw_height_parts("目の高さの棚の上の時計") == ["eye"]  # 置き場所の高さはそのまま
    assert strip_draw_viewpoint("目の高さで見た部屋") == "見た部屋"
    # 視点だけの draw は「床から離す」指定にならない
    assert not wants_off_floor("目の高さから見た正面の部屋の絵。床に置かれたラグの上のクッション")


def test_real_placement_problems_are_still_flagged_next_to_a_viewpoint():
    draw = "目の高さから見た部屋の絵。腰の高さの棚の上の加湿器1台"
    assert "low shelf" in find_placement_problem(
        draw, [], "An eye-level view of one humidifier on a low shelf"
    )
    assert "waist-high" in find_placement_problem(
        draw, [], "An eye-level view of one waist-high humidifier on a wooden shelf"
    )
    # 置き場所の高さの「目の高さ」は今までどおり見る
    assert find_placement_problem(
        "目の高さの棚の上の時計1台", [], "one eye-level clock hanging in the hallway"
    ) is not None


def test_avoid_opposites_are_positive_and_skip_what_is_said():
    assert avoid_opposite_phrases(["窓際に置かれている"]) == [
        "away from the window, toward the middle of the room"
    ]
    assert avoid_opposite_phrases(["窓際に置かれている"], "one humidifier toward the middle of the room") == []
    assert avoid_opposite_phrases(["子どもの手が届く低い台に置かれている"]) == [
        "up on a high shelf, well above the floor"
    ]
    # draw が高さを決めているときは high shelf と言わない
    assert avoid_opposite_phrases(["床に直置き", "低い台"], draw="腰の高さの台の上の加湿器") == [
        "raised up off the floor"
    ]
    assert avoid_opposite_phrases(["枕元に加湿器がある"]) == ["a few steps away from the bed, across the room"]
    assert avoid_opposite_phrases(["テレビの真下に加湿器がある"]) == [
        "on its own stand with open space around it, apart from other appliances"
    ]
    for phrase in ("raised up off the floor", "up on a high shelf, well above the floor"):
        assert find_placement_problem("腰の高さの台の上の加湿器", ["床"], f"one humidifier, {phrase}") is None


def test_draw_placement_phrases_keep_height_and_distance():
    assert draw_placement_phrases("目の高さから見た部屋。壁から30cm離した腰の高さの台の上の加湿器") == [
        "at waist height",
        "with about 30 centimeters of open space around it",
    ]
    assert draw_placement_phrases("ベッドから1メートル以上離れた棚") == [
        "with about 1 meter of open space around it"
    ]
    assert draw_placement_phrases("腰の高さの台、壁から30cm離す", "on a waist-height stand 30 cm from the wall") == []


@patch(FACTORY_PATH)
@pytest.mark.parametrize("draw,avoid,_prompt", HUMIDIFIER_SCENES)
def test_short_prompt_gets_avoid_opposite_and_placement(mock_factory_create, draw, avoid, _prompt):
    gen, port = _make_generator(mock_factory_create)
    port.generate = AsyncMock(side_effect=[
        _response(_long_body()), _response(_long_body(300)), _response("One white humidifier in a quiet room."),
    ])
    seg = ScriptSegment(
        segment_id="deep_dive_1_scene2",
        segment_type="deep_dive",
        topic_title="加湿器の置き場所",
        turns=[{"speaker": "A", "text": "加湿器は置き場所が大事なのだ。"}],
        scene_visual=SceneVisual(start_turn=0, focus="置き場所", draw=draw, avoid=avoid),
    )
    prompt = asyncio.run(gen.generate_prompt(seg))
    assert prompt.startswith(gen.SEGMENT_PROMPT_PREFIX + "One white humidifier in a quiet room, ")
    for phrase in short_prompt_placement_phrases(draw, avoid, "One white humidifier in a quiet room"):
        assert phrase in prompt
    assert short_prompt_placement_phrases(draw, avoid, "One white humidifier in a quiet room")
    assert find_placement_problem(draw, avoid, prompt) is None
    assert len(prompt) <= gen.MAX_SEGMENT_PROMPT_CHARS
    short_request = port.generate.call_args_list[2].args[0]
    assert "write the correct opposite place positively" in short_request.user_prompt
    assert "away from the window, toward the middle of the room" in short_request.system_prompt
    assert gen.warnings[0].endswith("used a short prompt built from focus/draw")


@patch(FACTORY_PATH)
def test_window_scene_short_prompt_says_away_from_window(mock_factory_create):
    draw, avoid, _ = HUMIDIFIER_SCENES[3]
    gen, port = _make_generator(mock_factory_create)
    port.generate = AsyncMock(side_effect=[
        _response(_long_body()), _response(_long_body()), _response("One humidifier on a wooden shelf."),
    ])
    seg = ScriptSegment(
        segment_id="deep_dive_2_scene1", segment_type="deep_dive", topic_title="t",
        turns=[{"speaker": "A", "text": "x"}],
        scene_visual=SceneVisual(start_turn=0, focus="f", draw=draw, avoid=avoid),
    )
    prompt = asyncio.run(gen.generate_prompt(seg))
    assert "One humidifier on a wooden shelf, at waist height, away from the window, toward the middle of the room" in prompt


# ---------------------------------------------------------------- B. 画像の provider (作り直し・使い回し)

def _scene(segment_id: str, draw: str = "腰の高さの棚の上の加湿器1台") -> ScriptSegment:
    return ScriptSegment(
        segment_id=segment_id,
        segment_type="deep_dive",
        topic_title="加湿器",
        turns=[{"speaker": "A", "text": "加湿器の話なのだ。"}],
        scene_visual=SceneVisual(start_turn=0, focus="置き場所", draw=draw, avoid=[]),
    )


def _provider(tmp_path, monkeypatch, calls, options=None, run="run"):
    """dynamic の ImageProvider。プロンプトは segment_id から決め、画像は seed と id を書いたファイル。"""
    monkeypatch.chdir(tmp_path)  # output/.image_cache を tmp に作る
    config = SimpleNamespace(yaml=SimpleNamespace(video_renderer=SimpleNamespace(background_mode="static")))
    provider = ImageProvider(config, output_dir=tmp_path / run)
    provider.mode = "dynamic"

    async def generate_prompt(segment, visual_identity=None):
        return f"A humidifier for {segment.segment_id}"

    provider.prompt_generator = SimpleNamespace(generate_prompt=generate_prompt, warnings=[])
    counter = {"n": 0}

    async def generate_image(prompt, output_path, **kwargs):
        counter["n"] += 1
        seed = kwargs.get("seed", 1000 + counter["n"])
        calls.append({"segment_id": kwargs["segment_id"], "seed_kwarg": kwargs.get("seed"), "path": output_path})
        output_path.write_bytes(f"{kwargs['segment_id']} seed={seed}".encode())
        return output_path, GenerationMetadata.create_from_segment(
            image_path=str(output_path), segment_id=kwargs["segment_id"], segment_type=kwargs["segment_type"],
            prompt=prompt, visual_identity={}, seed=seed, generation_time_sec=1.0,
            resolution="1024x576", steps=4, sampler="euler", cfg_scale=1.0,
        )

    provider.image_client = SimpleNamespace(generate_image=generate_image)
    if options is not None:
        provider.scene_options = options
    return provider


def _images_run(tmp_path, monkeypatch, ids, options=None, run="run1"):
    calls = []
    provider = _provider(tmp_path, monkeypatch, calls, options, run)
    paths = [asyncio.run(provider.get_image_for_segment(_scene(sid))) for sid in ids]
    provider.finish_scene_images()
    exported = export_scene_images(provider.scene_records, tmp_path / run)
    return provider, calls, paths, exported


IDS = ["deep_dive_1_scene1", "deep_dive_1_scene2", "deep_dive_2"]


def test_records_and_export(tmp_path, monkeypatch):
    provider, calls, paths, exported = _images_run(tmp_path, monkeypatch, IDS)
    assert [c["seed_kwarg"] for c in calls] == [None, None, None]  # 指定が無ければ seed は渡さない
    assert [r["segment_id"] for r in exported] == IDS
    assert [r["scene_index"] for r in exported] == [0, 1, 0]
    assert [r["seed"] for r in exported] == [1001, 1002, 1003]
    assert [r["source"] for r in exported] == ["generated"] * 3
    assert all(r["cached"] is False for r in exported)
    assert exported[0]["prompt"] == "A humidifier for deep_dive_1_scene1"
    assert exported[0]["scene_visual"]["draw"] == "腰の高さの棚の上の加湿器1台"
    run_dir = (tmp_path / "run1").resolve()
    assert exported[1]["image_path"] == str(run_dir / SCENE_IMAGES_DIR / "02_deep_dive_1_scene2.png")
    assert Path(exported[1]["image_path"]).read_bytes() == paths[1].read_bytes()
    manifest = json.loads((tmp_path / "run1" / SCENE_IMAGES_MANIFEST).read_text(encoding="utf-8"))
    assert manifest == {"scene_images": exported}
    assert provider.warnings == []

    # 2 回目は同じプロンプトなのでキャッシュ
    provider2, calls2, _, exported2 = _images_run(tmp_path, monkeypatch, IDS, run="run2")
    assert calls2 == []
    assert [r["source"] for r in exported2] == ["cache"] * 3
    assert [r["cached"] for r in exported2] == [True] * 3
    assert [r["seed"] for r in exported2] == [None] * 3


def test_regen_scenes_changes_only_that_scenes_seed(tmp_path, monkeypatch):
    _images_run(tmp_path, monkeypatch, IDS)  # キャッシュを作る
    options = build_scene_image_options(regen_scenes=["deep_dive_1_scene2"], regen_seed_offset=5)
    _, calls, paths, exported = _images_run(tmp_path, monkeypatch, IDS, options, run="run2")
    # キャッシュがあっても指定の場面だけ作り直す。元の seed が分からないので segment_id から決めた値 + 5
    assert calls == [{
        "segment_id": "deep_dive_1_scene2",
        "seed_kwarg": base_seed_for("deep_dive_1_scene2") + 5,
        "path": paths[1],
    }]
    assert paths[1].name.endswith(f"_seed{base_seed_for('deep_dive_1_scene2') + 5}.png")
    assert [r["source"] for r in exported] == ["cache", "generated", "cache"]
    assert exported[1]["seed"] == base_seed_for("deep_dive_1_scene2") + 5
    # 元のキャッシュの画像は書き換えない
    original = tmp_path / "output" / ".image_cache"
    assert len(list(original.glob("*.png"))) == 4


def test_reuse_images_from_uses_the_same_files(tmp_path, monkeypatch):
    _, _, _, first = _images_run(tmp_path, monkeypatch, IDS)
    options = build_scene_image_options(reuse_images_from=tmp_path / "run1")
    calls = []
    provider = _provider(tmp_path, monkeypatch, calls, options, run="run2")
    ids = IDS[:2] + ["deep_dive_3_scene1"]
    paths = [asyncio.run(provider.get_image_for_segment(_scene(sid))) for sid in ids]
    provider.finish_scene_images()

    assert paths[0] == Path(first[0]["image_path"]) and paths[1] == Path(first[1]["image_path"])
    assert [c["segment_id"] for c in calls] == ["deep_dive_3_scene1"]  # 対応しない場面だけ作る
    records = provider.scene_records
    assert [r["source"] for r in records] == ["reused", "reused", "generated"]
    assert records[0]["seed"] == 1001 and records[0]["prompt"] == first[0]["prompt"]
    run1 = tmp_path / "run1"
    assert provider.warnings == [
        f"reused 2 scene image(s) from {run1}: deep_dive_1_scene1, deep_dive_1_scene2",
        f"no reusable image in {run1} for 1 scene(s), generated as usual: deep_dive_3_scene1",
        f"1 scene image(s) of {run1} matched no scene of this run: deep_dive_2",
    ]


def test_reuse_and_regen_together(tmp_path, monkeypatch):
    _, _, _, first = _images_run(tmp_path, monkeypatch, IDS)
    options = build_scene_image_options(
        regen_scenes=["deep_dive_2"], regen_seed_offset=1, reuse_images_from=tmp_path / "run1",
    )
    _, calls, paths, exported = _images_run(tmp_path, monkeypatch, IDS, options, run="run2")
    # 前の run の seed (1003) + 1 で作り直し、他は前の画像のまま
    assert [(c["segment_id"], c["seed_kwarg"]) for c in calls] == [("deep_dive_2", 1004)]
    assert [r["source"] for r in exported] == ["reused", "reused", "generated"]
    assert paths[0] == Path(first[0]["image_path"])
    assert Path(exported[0]["image_path"]).read_bytes() == Path(first[0]["image_path"]).read_bytes()
    assert Path(exported[2]["image_path"]).read_bytes() == b"deep_dive_2 seed=1004"
    assert Path(first[2]["image_path"]).read_bytes() == b"deep_dive_2 seed=1003"  # 前の run は変えない


def test_reuse_from_a_dir_without_manifest_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="scene_images.json not found"):
        build_scene_image_options(reuse_images_from=tmp_path)


def test_reuse_finds_moved_run_dir(tmp_path, monkeypatch):
    _images_run(tmp_path, monkeypatch, IDS)
    moved = tmp_path / "moved"
    (tmp_path / "run1").rename(moved)
    options = build_scene_image_options(reuse_images_from=moved)
    assert options.reuse_entries["deep_dive_2"][0]["image_path"] == str(moved / SCENE_IMAGES_DIR / "03_deep_dive_2.png")


def test_helpers():
    assert scene_index_of("deep_dive_2_scene3") == 2
    assert scene_index_of("intro") == 0
    starts, end = estimate_turn_times([{"text": "あ" * 80}, {"text": "い" * 40 + "[src=1]"}], 1.0, 0.5)
    assert starts == [0.0, 10.5] and end == 15.5
    assert build_scene_image_options().active is False


def test_seed_is_sent_to_the_image_backends(mock_app_config):
    """Forge は payload の seed、ComfyUI は KSampler の seed に入る (指定が無ければ今までどおり乱数)。"""
    import inspect

    from services.media_processing.comfyui_client import ComfyUIClient
    from services.media_processing.flux_client import FluxClient

    for client in (FluxClient, ComfyUIClient):
        param = inspect.signature(client.generate_image).parameters["seed"]
        assert param.default is None


# ---------------------------------------------------------------- B. run_workflow_sync(images_only=True)

def _segments():
    def visual(start, draw):
        return SceneVisual(start_turn=start, focus="置き場所", draw=draw, avoid=["窓際に置かれている"])

    turns = [{"speaker": "A", "text": f"加湿器の話 {k} なのだ。"} for k in range(4)]
    return [
        ScriptSegment(segment_id="intro", segment_type="intro", topic_title=None, turns=turns[:1]),
        ScriptSegment(
            segment_id="deep_dive_1", segment_type="deep_dive", topic_title="加湿器の置き場所", turns=turns,
            visuals=[visual(0, "腰の高さの棚の上の加湿器1台"), visual(2, "部屋の中央寄りの台の上の加湿器1台")],
        ),
    ]


@pytest.fixture
def images_workflow(stubbed_workflow, monkeypatch, tmp_path):  # noqa: F811
    """台本は visuals のある 2 章、ImageProvider は生成を偽物にしたもの。"""
    import services.pipeline

    async def _external(**kwargs):
        return SimpleNamespace(
            verified_script=SimpleNamespace(metadata=SimpleNamespace(title="加湿器の回", references=[])),
            script=SimpleNamespace(sections=[], title="加湿器の回"),
            segments=_segments(),
            visual_identity=None,
            warnings=[],
        )

    monkeypatch.setattr(services.pipeline, "execute_external_script_phase", _external)
    calls = []

    class _FakeProvider(ImageProvider):
        def __init__(self, config, visual_identity=None, output_dir=None):
            fake = _provider(tmp_path, monkeypatch, calls, run=str(output_dir))
            self.__dict__.update(fake.__dict__)

    monkeypatch.setattr(services.media_processing, "ImageProvider", _FakeProvider)
    stubbed_workflow.image_calls = calls
    return stubbed_workflow


def test_images_only_stops_before_audio(images_workflow):
    result, logs = _run(images_only=True)
    assert result.success, result.error_message
    assert images_workflow.calls == {"production": 0, "metadata": 0, "thumbnail": 0}
    assert _FakeYouTubeClient.instances == []
    assert result.images_only is True and result.video_path is None and result.audio_path is None
    ids = [r["segment_id"] for r in result.scene_images]
    assert ids == ["intro", "deep_dive_1_scene1", "deep_dive_1_scene2"]
    fields = workflow.build_scene_images_result_fields(result)
    assert list(fields) == ["scene_images"]
    entry = fields["scene_images"][1]
    assert set(entry) == {
        "segment_id", "scene_index", "image_path", "scene_visual", "prompt", "seed", "cached", "source",
    }
    assert entry["scene_index"] == 0 and entry["scene_visual"]["draw"] == "腰の高さの棚の上の加湿器1台"
    image = Path(entry["image_path"])
    assert image.is_absolute() and image.is_file()
    assert image.parent == (result.output_dir / SCENE_IMAGES_DIR).resolve()
    assert (result.output_dir / SCENE_IMAGES_MANIFEST).is_file()
    assert workflow.build_upload_result_fields(result)["upload_status"] == "skipped"
    assert any("画像だけを作る実行" in m for m in logs)


def test_full_run_passes_scene_options_to_production(images_workflow, monkeypatch, tmp_path):
    first, _ = _run(images_only=True)
    seen = {}

    async def _production(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop after production is called")

    monkeypatch.setattr(workflow, "execute_production_phase", _production)
    result, _ = _run(
        reuse_images_from=str(first.output_dir), regen_scenes=["deep_dive_1_scene2"], regen_seed_offset=2,
    )
    assert not result.success
    options = seen["scene_options"]
    assert options.regen_scenes == {"deep_dive_1_scene2"} and options.regen_seed_offset == 2
    assert options.reuse_dir == first.output_dir
    assert sorted(options.reuse_entries) == ["deep_dive_1_scene1", "deep_dive_1_scene2", "intro"]


def test_default_run_passes_no_scene_options(images_workflow, monkeypatch):
    seen = {}
    original = workflow.execute_production_phase

    async def _production(**kwargs):
        seen.update(kwargs)
        return await original(**kwargs)

    monkeypatch.setattr(workflow, "execute_production_phase", _production)
    result, _ = _run(skip_upload=True)
    assert result.success, result.error_message
    assert "scene_options" not in seen
    assert result.images_only is False and result.scene_images == []
    assert workflow.build_scene_images_result_fields(result) == {"scene_images": []}


def test_reuse_from_missing_dir_fails_early(images_workflow, tmp_path):
    result, _ = _run(reuse_images_from=str(tmp_path / "nope"))
    assert not result.success
    assert "scene_images.json not found" in result.error_message
    assert images_workflow.calls["production"] == 0


def test_images_only_phase_reuses_previous_run(images_workflow, tmp_path, monkeypatch):
    """images_only を 2 回: 2 回目は 1 回目の画像を使い回し、指定の場面だけ seed をずらして作る。"""
    config = SimpleNamespace(yaml=SimpleNamespace(video_renderer=SimpleNamespace(background_mode="static")))
    first = asyncio.run(workflow.execute_images_only_phase(_segments(), config, tmp_path / "a"))
    images_workflow.image_calls.clear()
    options = build_scene_image_options(
        regen_scenes=["deep_dive_1_scene2"], regen_seed_offset=1, reuse_images_from=tmp_path / "a",
    )
    second = asyncio.run(workflow.execute_images_only_phase(_segments(), config, tmp_path / "b", scene_options=options))
    assert [c["segment_id"] for c in images_workflow.image_calls] == ["deep_dive_1_scene2"]
    assert images_workflow.image_calls[0]["seed_kwarg"] == first.scene_images[2]["seed"] + 1
    assert [r["source"] for r in second.scene_images] == ["reused", "reused", "generated"]
    for k in (0, 1):
        assert Path(second.scene_images[k]["image_path"]).read_bytes() == Path(
            first.scene_images[k]["image_path"]
        ).read_bytes()
    assert any(w.startswith("reused 2 scene image(s)") for w in second.warnings)
