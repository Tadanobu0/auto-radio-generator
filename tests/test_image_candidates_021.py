"""画像段で 1 場面あたり N 枚の候補を作り、一覧画像を出す (021)

- candidates=N (images_only): 各場面を seed 違いで N 枚。scene_images/NN_<id>_cK.png と一覧画像
  NN_<id>_sheet.jpg、scene_images.json の candidates / contact_sheet。既定 (1) は今までと同じ
- サムネイル背景も N 枚 + 一覧画像 (thumbnail_background_mode=dynamic のときだけ)
- picks: reuse_images_from の候補から使う番号 (指定が無い場面は候補 1)
- prompt_overrides: 作る場面のプロンプトを上書きし、上書きした旨と文を記録する
- 1 枚ごとの生成時間を記録する
実機の ComfyUI・GPU は使わない (画像は小さな PNG を書く偽物)。
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import services.media_processing
import workflow
from core.models.curation import ScriptSegment
from core.models.generation_metadata import GenerationMetadata
from services.media_processing.contact_sheet import grid_shape, make_contact_sheet, sheet_layout
from services.media_processing.image_provider import ImageProvider
from services.media_processing.scene_images import (
    SCENE_IMAGES_DIR,
    SCENE_IMAGES_MANIFEST,
    THUMBNAIL_CANDIDATES_DIR,
    base_seed_for,
    build_scene_image_options,
    export_scene_images,
    parse_candidates,
    parse_picks,
    parse_prompt_overrides,
)
from services.script_generation.image_prompt_generator import ImagePromptGenerator
from tests.test_scene_images_016 import _scene, _segments
from tests.test_workflow_no_upload import _run, stubbed_workflow  # noqa: F401

PREFIX = ImagePromptGenerator.SEGMENT_PROMPT_PREFIX
IDS = ["deep_dive_1_scene1", "deep_dive_1_scene2", "deep_dive_2"]


def _png(path: Path, seed: int, size=(64, 36)) -> Path:
    """seed で色の変わる小さな PNG。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (seed % 256, (seed // 256) % 256, 160)).save(path)
    return path


def _color(path) -> tuple:
    with Image.open(path) as image:
        return image.convert("RGB").getpixel((image.width - 1, image.height - 1))


# ---------------------------------------------------------------- 偽の ImageProvider

def _provider(tmp_path, monkeypatch, calls, options=None, run="run", fail_seeds=()):
    """dynamic の ImageProvider。プロンプトは segment_id から決め、画像は seed の色の PNG。"""
    monkeypatch.chdir(tmp_path)
    config = SimpleNamespace(yaml=SimpleNamespace(video_renderer=SimpleNamespace(background_mode="static")))
    provider = ImageProvider(config, output_dir=tmp_path / run)
    provider.mode = "dynamic"
    prompts = []

    async def generate_prompt(segment, visual_identity=None):
        prompts.append(segment)
        return f"A humidifier for {segment.segment_id}"

    provider.prompt_generator = SimpleNamespace(generate_prompt=generate_prompt, warnings=[])
    provider.prompt_calls = prompts
    counter = {"n": 0}

    async def generate_image(prompt, output_path, **kwargs):
        counter["n"] += 1
        seed = kwargs.get("seed", 1000 + counter["n"])
        calls.append({"segment_id": kwargs["segment_id"], "seed": kwargs.get("seed"), "prompt": prompt})
        if seed in fail_seeds:
            raise RuntimeError(f"ComfyUI failed for seed {seed}")
        _png(output_path, seed)
        return output_path, GenerationMetadata.create_from_segment(
            image_path=str(output_path), segment_id=kwargs["segment_id"], segment_type=kwargs["segment_type"],
            prompt=prompt, visual_identity={}, seed=seed, generation_time_sec=1.0,
            resolution="64x36", steps=4, sampler="euler", cfg_scale=1.0,
        )

    provider.image_client = SimpleNamespace(generate_image=generate_image)
    if options is not None:
        provider.scene_options = options
    return provider


def _images_run(tmp_path, monkeypatch, ids=IDS, options=None, run="run1", fail_seeds=()):
    calls = []
    provider = _provider(tmp_path, monkeypatch, calls, options, run, fail_seeds)
    paths = [asyncio.run(provider.get_image_for_segment(_scene(sid))) for sid in ids]
    provider.finish_scene_images()
    warnings = []
    exported = export_scene_images(provider.scene_records, tmp_path / run, warnings)
    provider.warnings.extend(warnings)
    return provider, calls, paths, exported


# ---------------------------------------------------------------- 引数の読み方

def test_parse_candidates():
    assert parse_candidates(None) == 1
    assert parse_candidates(9) == 9
    assert parse_candidates("4") == 4
    for bad in (0, 17, True, "x", -1):
        with pytest.raises(ValueError, match="candidates"):
            parse_candidates(bad)


def test_parse_picks_from_dict_and_file(tmp_path):
    assert parse_picks(None) == {}
    assert parse_picks({"deep_dive_1_scene1": 3, "thumbnail": "2"}) == {"deep_dive_1_scene1": 3, "thumbnail": 2}
    path = tmp_path / "picks.json"
    path.write_text(json.dumps({"picks": {"intro": 5}}), encoding="utf-8")
    assert parse_picks(str(path)) == {"intro": 5}
    for bad in ({"intro": 0}, {"intro": "a"}, {"intro": True}, {"intro": 1.5}):
        with pytest.raises(ValueError, match="picks"):
            parse_picks(bad)
    with pytest.raises(ValueError, match="not found"):
        parse_picks(str(tmp_path / "none.json"))
    with pytest.raises(ValueError, match="reuse_images_from is required"):
        build_scene_image_options(picks={"intro": 2})


def test_parse_prompt_overrides(tmp_path):
    assert parse_prompt_overrides(None) == {}
    got = parse_prompt_overrides({
        "intro": "  one white humidifier on a shelf  ",
        "deep_dive_1_scene2": {"draw": "腰の高さの棚の上の加湿器1台", "avoid": "窓際"},
        "thumbnail": "a bright living room",
    })
    assert got == {
        "intro": "one white humidifier on a shelf",
        "deep_dive_1_scene2": {"draw": "腰の高さの棚の上の加湿器1台", "avoid": "窓際"},
        "thumbnail": "a bright living room",
    }
    path = tmp_path / "prompt_overrides.json"
    path.write_text(json.dumps({"prompt_overrides": {"intro": "x"}}), encoding="utf-8")
    assert parse_prompt_overrides(path) == {"intro": "x"}
    for bad in ({"intro": ""}, {"intro": {"colour": "red"}}, {"thumbnail": {"draw": "x"}},
                {"intro": {"draw": ""}}, {"intro": 3}, {"intro": {"avoid": [1]}}):
        with pytest.raises(ValueError, match="prompt_overrides"):
            parse_prompt_overrides(bad)


def test_candidate_seeds_follow_the_regen_offset_rule():
    options = build_scene_image_options(candidates=3, regen_seed_offset=5)
    base = base_seed_for("deep_dive_2")
    assert options.candidate_seeds("deep_dive_2", None, regen=False) == [base, base + 1, base + 2]
    previous = {"seed": 10, "candidates": [{"index": 1, "seed": 10}, {"index": 2, "seed": 12}, {"index": 3, "seed": 11}]}
    # 作り直しは前の候補の一番大きい seed + offset から続ける (前の絵が戻ってこない)
    assert options.candidate_seeds("deep_dive_2", previous, regen=True) == [17, 18, 19]
    assert options.candidate_seeds("deep_dive_2", None, regen=True) == [base + 5, base + 6, base + 7]
    # 1 枚の作り直しの seed も前の候補の後ろになる (016 の流儀はそのまま)
    assert options.regen_seed("deep_dive_2", previous) == 17
    assert options.regen_seed("deep_dive_2", {"seed": 10}) == 15


# ---------------------------------------------------------------- 一覧画像

def test_grid_and_layout():
    assert grid_shape(9) == (3, 3)
    assert grid_shape(4) == (2, 2)
    assert grid_shape(5) == (3, 2)
    assert grid_shape(2) == (2, 1)
    assert grid_shape(1) == (1, 1)
    layout = sheet_layout(9, 16 / 9)
    assert layout["width"] == 2048 and layout["height"] < 2048
    assert (layout["tile_w"], layout["tile_h"]) == (672, 378)
    tall = sheet_layout(2, 0.5)  # 縦長の絵 2 枚: 横に並べても高さが長辺
    assert max(tall["width"], tall["height"]) <= 2048


def test_contact_sheet_has_tiles_and_numbers(tmp_path):
    items = [(k, _png(tmp_path / f"c{k}.png", 40 * k, size=(160, 90))) for k in range(1, 9)]
    items.append((9, None))  # 失敗した候補は灰色のタイル
    out = make_contact_sheet(items, tmp_path / "sheet.jpg")
    layout = sheet_layout(9, 16 / 9)
    with Image.open(out) as sheet:
        assert sheet.format == "JPEG"
        assert sheet.size == (layout["width"], layout["height"])
        rgb = sheet.convert("RGB")
        # 番号の箱 (黒地に白) が各タイルの左上にある
        x0, y0 = 8, 8
        box = [rgb.getpixel((x0 + dx, y0 + dy)) for dx in range(0, 120, 2) for dy in range(0, 120, 2)]
        assert any(sum(p) > 600 for p in box) and any(sum(p) < 60 for p in box)
        # タイルの右下は候補の絵の色
        tw, th = layout["tile_w"], layout["tile_h"]
        assert abs(rgb.getpixel((x0 + tw - 10, y0 + th - 10))[0] - 40) < 12
    with pytest.raises(ValueError):
        make_contact_sheet([], tmp_path / "empty.jpg")


# ---------------------------------------------------------------- ImageProvider: 候補を作る

def test_candidates_are_generated_with_consecutive_seeds_and_exported(tmp_path, monkeypatch):
    options = build_scene_image_options(candidates=3)
    provider, calls, paths, exported = _images_run(tmp_path, monkeypatch, options=options)
    for k, sid in enumerate(IDS):
        base = base_seed_for(sid)
        assert [c["seed"] for c in calls[3 * k:3 * k + 3]] == [base, base + 1, base + 2]
        assert {c["segment_id"] for c in calls[3 * k:3 * k + 3]} == {sid}
    assert len(provider.prompt_calls) == 3  # プロンプトは場面ごとに 1 回

    run_dir = (tmp_path / "run1").resolve()
    entry = exported[1]
    assert entry["source"] == "generated" and entry["seed"] == base_seed_for(IDS[1])
    assert [c["index"] for c in entry["candidates"]] == [1, 2, 3]
    for cand in entry["candidates"]:
        assert cand["image_path"] == str(run_dir / SCENE_IMAGES_DIR / f"02_deep_dive_1_scene2_c{cand['index']}.png")
        assert cand["cached"] is False and cand["generation_time_sec"] >= 0.0
        assert "error" not in cand
    # 場面の画像 (今までのキー) は候補 1
    assert entry["image_path"] == str(run_dir / SCENE_IMAGES_DIR / "02_deep_dive_1_scene2.png")
    assert _color(entry["image_path"]) == _color(entry["candidates"][0]["image_path"])
    assert entry["contact_sheet"] == str(run_dir / SCENE_IMAGES_DIR / "02_deep_dive_1_scene2_sheet.jpg")
    with Image.open(entry["contact_sheet"]) as sheet:
        assert max(sheet.size) == 2048
    manifest = json.loads((tmp_path / "run1" / SCENE_IMAGES_MANIFEST).read_text(encoding="utf-8"))
    assert manifest == {"scene_images": exported}
    assert provider.warnings == []


def test_same_prompt_and_seed_comes_from_the_cache(tmp_path, monkeypatch):
    options = build_scene_image_options(candidates=3)
    _images_run(tmp_path, monkeypatch, options=options)
    options = build_scene_image_options(candidates=3)
    _, calls, _, exported = _images_run(tmp_path, monkeypatch, options=options, run="run2")
    assert calls == []
    assert [r["source"] for r in exported] == ["cache"] * 3
    assert all(c["cached"] and c["generation_time_sec"] == 0.0 for r in exported for c in r["candidates"])


def test_regen_with_candidates_after_reuse(tmp_path, monkeypatch):
    _, _, _, first = _images_run(tmp_path, monkeypatch, options=build_scene_image_options(candidates=3))
    options = build_scene_image_options(
        candidates=3, regen_scenes=["deep_dive_2"], regen_seed_offset=1, reuse_images_from=tmp_path / "run1",
    )
    provider, calls, _, exported = _images_run(tmp_path, monkeypatch, options=options, run="run2")
    top = max(c["seed"] for c in first[2]["candidates"])
    assert [(c["segment_id"], c["seed"]) for c in calls] == [("deep_dive_2", top + k) for k in (1, 2, 3)]
    assert [r["source"] for r in exported] == ["reused", "reused", "generated"]
    # 使い回した場面も候補ごと新しい run に写り、一覧画像も作り直される
    run2 = (tmp_path / "run2").resolve()
    reused = exported[0]
    assert [Path(c["image_path"]).parent for c in reused["candidates"]] == [run2 / SCENE_IMAGES_DIR] * 3
    for old, new in zip(first[0]["candidates"], reused["candidates"]):
        assert _color(old["image_path"]) == _color(new["image_path"]) and old["seed"] == new["seed"]
    assert Path(reused["contact_sheet"]).parent == run2 / SCENE_IMAGES_DIR
    assert any(w.startswith("reused 2 scene image(s)") for w in provider.warnings)


def test_a_failed_candidate_is_kept_as_a_hole(tmp_path, monkeypatch):
    base = base_seed_for("deep_dive_2")
    options = build_scene_image_options(candidates=3)
    provider, _, _, exported = _images_run(
        tmp_path, monkeypatch, ids=["deep_dive_2"], options=options, fail_seeds={base},
    )
    entry = exported[0]
    first, second, third = entry["candidates"]
    assert first["image_path"] is None and "seed " + str(base) in first["error"]
    assert second["image_path"] and third["image_path"]
    assert entry["seed"] == base + 1 and _color(entry["image_path"]) == _color(second["image_path"])
    assert Path(entry["contact_sheet"]).is_file()
    assert provider.warnings == ["1 of 3 candidates of deep_dive_2 failed (candidate 1); the others are kept"]


def test_all_candidates_failing_uses_a_static_image(tmp_path, monkeypatch):
    base = base_seed_for("deep_dive_2")
    calls = []
    options = build_scene_image_options(candidates=2)
    provider = _provider(tmp_path, monkeypatch, calls, options, fail_seeds={base, base + 1})
    static = _png(tmp_path / "static.png", 1)
    provider._static_images = {"generic": [static]}
    path = asyncio.run(provider.get_image_for_segment(_scene("deep_dive_2")))
    assert path == static
    record = provider.scene_records[0]
    assert record["source"] == "static" and len(record["candidates"]) == 2
    assert "all 2 candidates failed" in provider.warnings[0] and provider.warnings[0].endswith("used a static image")


def test_default_records_have_no_new_keys(tmp_path, monkeypatch):
    """候補数 1 (既定) は 016 と同じ形。作り直しの seed も今までどおり。"""
    options = build_scene_image_options(regen_scenes=["deep_dive_2"], regen_seed_offset=2)
    _, calls, _, exported = _images_run(tmp_path, monkeypatch, options=options)
    assert len(calls) == 3
    assert calls[2]["seed"] == base_seed_for("deep_dive_2") + 2
    for entry in exported:
        assert set(entry) == {
            "segment_id", "scene_index", "image_path", "scene_visual", "prompt", "seed", "cached", "source",
        }
    assert sorted(p.name for p in (tmp_path / "run1" / SCENE_IMAGES_DIR).iterdir()) == [
        "01_deep_dive_1_scene1.png", "02_deep_dive_1_scene2.png", "03_deep_dive_2.png",
    ]


# ---------------------------------------------------------------- picks

def test_picks_choose_the_reused_candidate(tmp_path, monkeypatch):
    _, _, _, first = _images_run(tmp_path, monkeypatch, options=build_scene_image_options(candidates=3))
    options = build_scene_image_options(
        reuse_images_from=tmp_path / "run1", picks={"deep_dive_1_scene2": 3, "deep_dive_9": 2},
    )
    calls = []
    provider = _provider(tmp_path, monkeypatch, calls, options, run="render")
    paths = [asyncio.run(provider.get_image_for_segment(_scene(sid))) for sid in IDS]
    provider.finish_scene_images()
    assert calls == []
    # 指定の場面は候補 3、指定の無い場面は候補 1 (= 場面の画像)
    assert paths[1] == Path(first[1]["candidates"][2]["image_path"])
    assert paths[0] == Path(first[0]["image_path"])
    records = provider.scene_records
    assert records[1]["picked"] == 3 and records[1]["seed"] == first[1]["candidates"][2]["seed"]
    assert "picked" not in records[0]
    assert "picks matched no reused scene of this run: deep_dive_9" in provider.warnings


def test_a_pick_that_does_not_exist_fails_early(tmp_path, monkeypatch):
    _images_run(tmp_path, monkeypatch, options=build_scene_image_options(candidates=2))
    with pytest.raises(ValueError, match="deep_dive_2 has no candidate 3"):
        build_scene_image_options(reuse_images_from=tmp_path / "run1", picks={"deep_dive_2": 3})
    # 候補の無い run (016 の形) の場面は候補 1 だけ
    _images_run(tmp_path, monkeypatch, run="plain")
    build_scene_image_options(reuse_images_from=tmp_path / "plain", picks={"deep_dive_2": 1})
    with pytest.raises(ValueError, match="no candidate 2"):
        build_scene_image_options(reuse_images_from=tmp_path / "plain", picks={"deep_dive_2": 2})
    with pytest.raises(ValueError, match="no thumbnail background candidate 1"):
        build_scene_image_options(reuse_images_from=tmp_path / "plain", picks={"thumbnail": 1})


# ---------------------------------------------------------------- prompt_overrides

def test_text_override_is_the_prompt_and_is_recorded(tmp_path, monkeypatch):
    _images_run(tmp_path, monkeypatch, options=build_scene_image_options(candidates=2))
    options = build_scene_image_options(
        candidates=2, regen_scenes=["deep_dive_2"], reuse_images_from=tmp_path / "run1",
        prompt_overrides={
            "deep_dive_2": "one white humidifier on a waist-height shelf",
            "deep_dive_1_scene1": "not used: this scene is reused",
            "outro": "matches nothing",
        },
    )
    provider, calls, _, exported = _images_run(tmp_path, monkeypatch, options=options, run="run2")
    assert [c["prompt"] for c in calls] == [PREFIX + "one white humidifier on a waist-height shelf"] * 2
    assert [s.segment_id for s in provider.prompt_calls] == []  # LLM のプロンプト作りは呼ばない
    entry = exported[2]
    assert entry["prompt"] == PREFIX + "one white humidifier on a waist-height shelf"
    assert entry["prompt_override"] == {"kind": "prompt", "text": "one white humidifier on a waist-height shelf"}
    assert "prompt_override" not in exported[0]
    assert any(w.startswith("prompt override not used because the scene image was reused") and
               "deep_dive_1_scene1" in w for w in provider.warnings)
    assert "prompt overrides matched no generated scene of this run: outro" in provider.warnings
    # 上書きの記録は次の run の使い回しにも残る
    options = build_scene_image_options(reuse_images_from=tmp_path / "run2")
    provider3, _, _, again = _images_run(tmp_path, monkeypatch, options=options, run="run3")
    assert again[2]["prompt_override"]["kind"] == "prompt"


def test_text_override_keeps_a_given_prefix_and_works_with_one_candidate(tmp_path, monkeypatch):
    options = build_scene_image_options(prompt_overrides={"deep_dive_2": PREFIX + "a lamp"})
    _, calls, _, exported = _images_run(tmp_path, monkeypatch, ids=["deep_dive_2"], options=options)
    assert calls[0]["prompt"] == PREFIX + "a lamp" and calls[0]["seed"] is None
    assert exported[0]["prompt_override"] == {"kind": "prompt", "text": PREFIX + "a lamp"}
    assert "candidates" not in exported[0]


def test_scene_direction_override_goes_through_the_prompt_generator(tmp_path, monkeypatch):
    options = build_scene_image_options(
        candidates=2, prompt_overrides={"deep_dive_2": {"draw": "胸の高さのチェストの上の加湿器1台"}},
    )
    provider, _, _, exported = _images_run(tmp_path, monkeypatch, ids=["deep_dive_2"], options=options)
    [segment] = provider.prompt_calls
    assert segment.scene_visual.draw == "胸の高さのチェストの上の加湿器1台"
    assert segment.scene_visual.focus == "置き場所"  # 上書きしていない欄はそのまま
    override = exported[0]["prompt_override"]
    assert override["kind"] == "scene_visual"
    assert override["scene_visual"]["draw"] == "胸の高さのチェストの上の加湿器1台"
    # 台本の場面そのものは変えない
    assert exported[0]["scene_visual"]["draw"] == "腰の高さの棚の上の加湿器1台"


def test_scene_direction_override_on_a_scene_without_visuals(tmp_path, monkeypatch):
    options = build_scene_image_options(prompt_overrides={"intro": {"draw": "机の上のラジオ1台"}})
    calls = []
    provider = _provider(tmp_path, monkeypatch, calls, options)
    segment = ScriptSegment(segment_id="intro", segment_type="intro", topic_title=None,
                            turns=[{"speaker": "A", "text": "こんにちは"}])
    asyncio.run(provider.get_image_for_segment(segment))
    assert provider.prompt_calls[0].scene_visual.draw == "机の上のラジオ1台"
    assert segment.scene_visual is None


# ---------------------------------------------------------------- run_workflow_sync

class _FakeThumbGenerator:
    instances = []

    def __init__(self, config, output_dir=None):
        self.output_dir = output_dir
        self.prompts = []
        self.calls = []
        _FakeThumbGenerator.instances.append(self)

    async def build_prompt(self, theme, script_summary, visual_identity=None, topic_title=None):
        self.prompts.append(topic_title)
        return f"thumbnail prompt for {topic_title}"

    async def generate(self, theme, script_summary, output_path, visual_identity=None, topic_title=None,
                       prompt=None, seed=None):
        self.calls.append({"output_path": output_path, "prompt": prompt, "seed": seed})
        return _png(output_path, seed or 0)


@pytest.fixture
def cand_workflow(stubbed_workflow, monkeypatch, tmp_path):  # noqa: F811
    """台本は 016 と同じ 2 章 (3 場面)、画像とサムネイル背景の生成は偽物。サムネイル背景は dynamic。"""
    import services.pipeline

    async def _external(**kwargs):
        return SimpleNamespace(
            verified_script=SimpleNamespace(metadata=SimpleNamespace(title="加湿器の回", references=[])),
            script=SimpleNamespace(sections=[], title="加湿器の回", description="加湿器の置き場所の話"),
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
    _FakeThumbGenerator.instances = []
    monkeypatch.setattr(workflow, "ThumbnailBackgroundGenerator", _FakeThumbGenerator)
    config = workflow.load_config(None)
    monkeypatch.setattr(
        config.yaml, "video_renderer", SimpleNamespace(thumbnail_background_mode="dynamic"), raising=False,
    )
    stubbed_workflow.image_calls = calls
    stubbed_workflow.tmp_path = tmp_path
    return stubbed_workflow


def _move(result, tmp_path, name):
    """run の出力ディレクトリを移す (同じ秒の 2 回目の実行と出力先が重ならないように)。"""
    target = tmp_path / name
    Path(result.output_dir).rename(target)
    return target


def test_images_only_with_candidates(cand_workflow, tmp_path):
    result, logs = _run(images_only=True, candidates=4)
    assert result.success, result.error_message
    assert cand_workflow.calls == {"production": 0, "metadata": 0, "thumbnail": 0}
    assert len(cand_workflow.image_calls) == 3 * 4
    assert result.image_candidates == 4
    run_dir = result.output_dir
    for entry in result.scene_images:
        assert len(entry["candidates"]) == 4
        assert all(Path(c["image_path"]).is_file() for c in entry["candidates"])
        assert Path(entry["contact_sheet"]).is_file()
    manifest = json.loads((run_dir / SCENE_IMAGES_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["scene_images"] == result.scene_images

    # サムネイル背景も 4 枚 + 一覧画像。thumbnail_bg.png は候補 1
    [gen] = _FakeThumbGenerator.instances
    assert gen.prompts == ["加湿器の回"]  # プロンプトは 1 回だけ作る
    base = base_seed_for("thumbnail")
    assert [c["seed"] for c in gen.calls] == [base, base + 1, base + 2, base + 3]
    assert {c["prompt"] for c in gen.calls} == {"thumbnail prompt for 加湿器の回"}
    thumb = manifest["thumbnail_bg"]
    assert thumb["image_path"] == str((run_dir / "thumbnail_bg.png").resolve())
    assert [Path(c["image_path"]).name for c in thumb["candidates"]] == [f"thumbnail_bg_c{k}.png" for k in range(1, 5)]
    assert Path(thumb["candidates"][0]["image_path"]).parent == (run_dir / THUMBNAIL_CANDIDATES_DIR).resolve()
    assert Path(thumb["contact_sheet"]).name == "thumbnail_bg_sheet.jpg"
    assert _color(run_dir / "thumbnail_bg.png") == _color(thumb["candidates"][0]["image_path"])
    assert result.thumbnail_bg_path == run_dir / "thumbnail_bg.png"

    fields = workflow.build_image_candidates_result_fields(result)
    assert fields["image_candidates"] == 4
    assert fields["thumbnail_bg_contact_sheet"] == thumb["contact_sheet"]
    times = fields["image_generation_times"]
    assert times["count"] == 3 * 4 + 4
    assert len(times["per_image"]) == 16 and times["mean_sec"] is not None
    assert {p["segment_id"] for p in times["per_image"]} == {
        "intro", "deep_dive_1_scene1", "deep_dive_1_scene2", "thumbnail",
    }
    assert any("4 枚の候補" in m for m in logs)


def test_default_images_only_output_is_unchanged(cand_workflow):
    result, _ = _run(images_only=True)
    assert result.success, result.error_message
    assert len(cand_workflow.image_calls) == 3
    manifest = json.loads((result.output_dir / SCENE_IMAGES_MANIFEST).read_text(encoding="utf-8"))
    assert list(manifest) == ["scene_images"]
    assert all("candidates" not in e and "contact_sheet" not in e for e in manifest["scene_images"])
    assert not (result.output_dir / THUMBNAIL_CANDIDATES_DIR).exists()
    fields = workflow.build_image_candidates_result_fields(result)
    assert fields["image_candidates"] == 1 and fields["thumbnail_bg_candidates"] == []
    assert fields["image_generation_times"]["count"] == 0


def test_candidates_are_only_for_images_only(cand_workflow):
    result, _ = _run(candidates=4)
    assert not result.success and "only for images_only" in result.error_message
    assert cand_workflow.calls["production"] == 0
    result, _ = _run(images_only=True, candidates=0)
    assert not result.success and "candidates" in result.error_message
    assert cand_workflow.image_calls == []


def test_second_images_run_reuses_thumbnail_candidates_and_regens_on_request(cand_workflow, tmp_path):
    first, _ = _run(images_only=True, candidates=3)
    run1 = _move(first, tmp_path, "run1")
    _FakeThumbGenerator.instances = []
    second, _ = _run(
        images_only=True, candidates=3, reuse_images_from=str(run1), regen_scenes=["deep_dive_1_scene2"],
        picks={"thumbnail": 2},
    )
    assert second.success, second.error_message
    assert _FakeThumbGenerator.instances == []  # サムネイル背景は作り直さない
    thumb = second.thumbnail_bg_candidates
    assert len(thumb) == 3 and all(c["reused"] for c in thumb)
    assert second.thumbnail_bg_pick == 2
    assert _color(second.thumbnail_bg_path) == _color(thumb[1]["image_path"])
    assert [r["source"] for r in second.scene_images] == ["reused", "reused", "generated"]
    fields = workflow.build_image_candidates_result_fields(second)
    assert fields["image_generation_times"]["count"] == 3  # 作り直した 1 場面の 3 枚だけ

    run2 = _move(second, tmp_path, "run2")
    third, _ = _run(
        images_only=True, candidates=3, reuse_images_from=str(run2), regen_scenes=["thumbnail"],
        prompt_overrides={"thumbnail": "a calm living room with one humidifier"},
    )
    assert third.success, third.error_message
    [gen] = _FakeThumbGenerator.instances
    assert gen.prompts == []  # 上書きの文をそのまま使う
    assert {c["prompt"] for c in gen.calls} == {"a calm living room with one humidifier"}
    top = max(c["seed"] for c in first.thumbnail_bg_candidates)
    assert [c["seed"] for c in gen.calls] == [top + 1, top + 2, top + 3]
    manifest = json.loads((third.output_dir / SCENE_IMAGES_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["thumbnail_bg"]["prompt_override"] == {
        "kind": "prompt", "text": "a calm living room with one humidifier",
    }


def test_full_run_takes_picks_for_scenes_and_thumbnail(cand_workflow, monkeypatch, tmp_path):
    first, _ = _run(images_only=True, candidates=3)
    run1 = _move(first, tmp_path, "run1")
    seen = {}

    async def _production(**kwargs):
        seen["scene_options"] = kwargs.get("scene_options")
        return SimpleNamespace(
            video_path=tmp_path / "video.mp4", audio_path=tmp_path / "a.wav", subtitle_path=tmp_path / "s.ass",
            duration_sec=60.0, file_size_mb=5.0, chapters=[], voicevox_usage=None,
            audio_duration_sec=60.0, render_duration_sec=10.0, segment_bg_generation_time=0.0,
        )

    async def _thumbnail(**kwargs):
        seen["thumbnail_bg_override"] = kwargs.get("thumbnail_bg_override")
        return kwargs["output_base"] / "thumbnail.png", 0.0

    monkeypatch.setattr(workflow, "execute_production_phase", _production)
    monkeypatch.setattr(workflow, "_generate_thumbnail_assets", _thumbnail)
    picks = tmp_path / "picks.json"
    picks.write_text(json.dumps({"deep_dive_1_scene1": 2, "thumbnail": 3}), encoding="utf-8")
    result, logs = _run(reuse_images_from=str(run1), picks=str(picks), skip_upload=True)
    assert result.success, result.error_message
    options = seen["scene_options"]
    assert options.picks == {"deep_dive_1_scene1": 2, "thumbnail": 3}
    assert options.candidates == 1
    expected = run1 / THUMBNAIL_CANDIDATES_DIR / "thumbnail_bg_c3.png"
    assert seen["thumbnail_bg_override"] == expected
    assert result.thumbnail_bg_pick == 3
    assert result.thumbnail_bg_source == str(expected)
    assert any("候補 3" in m for m in logs)


def test_full_run_with_a_missing_pick_fails_before_production(cand_workflow, tmp_path):
    first, _ = _run(images_only=True, candidates=2)
    run1 = _move(first, tmp_path, "run1")
    result, _ = _run(reuse_images_from=str(run1), picks={"intro": 5}, skip_upload=True)
    assert not result.success and "intro has no candidate 5" in result.error_message
    assert cand_workflow.calls["production"] == 0
