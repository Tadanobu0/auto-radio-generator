"""台本の visuals 欄で場面を切り、画像プロンプトに使うことのテスト (011、spec v1.11.0 §12)

- visuals の無い台本 (直近の実台本 3 本) は、場面の区切りも画像プロンプト生成への入力も変更前と同じ (1-a)
- 不正な visuals (範囲外・逆順・場面 0・5 場面など) は、その章だけ scene_splitter に戻り、警告が出る。
  ほかの章は visuals どおり (1-b)
- visuals がある章は start_turn で切り、各場面の focus / draw / avoid がプロンプトの指示に入る。
  avoid はネガティブプロンプトにせず、逆の状態を描くよう指示する
"""
import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import anyio
import pytest

from core.interfaces import SegmentTiming, SynthesisResult
from core.interfaces.llm_port import LLMResponse
from core.models.curation import SceneVisual, ScriptSegment
from core.models.usage import LLMUsage
from core.models.verified_script import VerifiedScript
from services.script_loading import RadioDirectorScriptLoader
from services.script_loading.radio_director_loader import build_script_segments, parse_scene_visuals
from services.video_rendering.scene_splitter import split_scene_starts
from services.video_rendering.timeline_calculator import TimelineCalculator

REAL_SCRIPTS_DIR = Path(__file__).parent / "fixtures" / "real_scripts"
# 直近の 3 本 (1-a)
RECENT_SCRIPTS = [
    "20261006_llmglm_frontier.json",
    "20261007_hermesagent_frontier.json",
    "20261011_style-bert-vits2.json",
]
BASE_SCRIPT = REAL_SCRIPTS_DIR / "20261006_llmglm_frontier.json"
FACTORY_PATH = "services.script_generation.image_prompt_generator.LLMAdapterFactory.create"
LOADER_LOGGER = "services.script_loading.radio_director_loader"

# 変更前 (010 時点) の user メッセージ。visuals の無い場面は 1 字も変わらないこと
OLD_USER_MESSAGE = """Generate a SCENE-DRIVEN cinematic image prompt for this radio segment.

Segment Type: {segment_type}
Topic: {topic}

Script Excerpts:
{context}

INSTRUCTIONS:
1. Read the excerpts (taken from across the whole segment) and identify the CORE IDEA of the segment
2. Choose ONE physical place or object arrangement that shows that idea - preferably the actual object the script discusses
3. Start your prompt with the hero object and state its size, material, where it is placed, and that it fills 40-60 percent of the frame
4. Add light, composition, and the unified visual brand (colors, aesthetic, film quality)
5. Keep the scene unpopulated and wordless, describing only what is in it; never copy names or figures from the script

Generate the prompt now:"""


# ---------------------------------------------------------------- 道具

def _load_segments(raw: dict) -> list[ScriptSegment]:
    return build_script_segments(VerifiedScript.model_validate_json(json.dumps(raw, ensure_ascii=False)))


def _raw(path: Path = BASE_SCRIPT) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _deep_dive_indices(raw: dict) -> list[int]:
    return [i for i, seg in enumerate(raw["script"]["segments"]) if seg["segment_type"] == "deep_dive"]


def _timings(segments: list[ScriptSegment]) -> tuple[list[SegmentTiming], float]:
    """読み上げの長さを字数から見積もったタイミング (1 字 0.13 秒、発話の間 0.25 秒、章の間 3 秒)。"""
    t = 2.0
    timings = []
    for seg in segments:
        seg_start = t
        starts = []
        for turn in seg.turns:
            starts.append(t)
            t += 0.13 * len(turn["text"]) + 0.25
        end = t - 0.25
        timings.append(SegmentTiming(
            segment_id=seg.segment_id,
            segment_type=seg.segment_type,
            topic_title=seg.topic_title,
            start_sec=seg_start,
            end_sec=end,
            duration_sec=end - seg_start,
            turn_start_secs=starts,
        ))
        t += 3.0
    return timings, t


def _run_timeline(mock_app_config, segments, timings=None):
    if timings is None:
        timings, total_sec = _timings(segments)
    else:
        total_sec = timings[-1].end_sec + 2.0
    asked: list[ScriptSegment] = []

    async def get_image(seg):
        asked.append(seg)
        return Path(f"bg_{seg.segment_id}.png")

    image_provider = MagicMock()
    image_provider.get_image_for_segment = get_image
    calc = TimelineCalculator(mock_app_config)

    async def run():
        return await calc.calculate_timeline(
            segments=segments,
            synthesis_result=SynthesisResult(
                audio_path=Path("audio.wav"),
                subtitle_path=Path("subs.ass"),
                total_duration_sec=total_sec,
                segment_timings=timings,
            ),
            image_provider=image_provider,
            jingle_provider=MagicMock(),
            bgm_path=Path("bgm.wav"),
        )

    return anyio.run(run), asked, timings


def _make_generator(mock_factory_create, response_text: str = "Two small white routers on a wooden shelf"):
    from services.script_generation.image_prompt_generator import ImagePromptGenerator

    port = Mock()
    port.generate = AsyncMock(return_value=LLMResponse(
        content=response_text,
        usage=LLMUsage(provider="ollama", model_name="m", input_tokens=1, output_tokens=1, request_count=1),
        finish_reason="stop",
    ))
    mock_factory_create.return_value = port
    config = Mock()
    config.yaml.script_generator.orchestrator.curator_model = "m"
    return ImagePromptGenerator(config), port


def _visual(start_turn, focus="要点", draw="描くもの", avoid=None) -> dict:
    item = {"start_turn": start_turn, "focus": focus, "draw": draw}
    if avoid is not None:
        item["avoid"] = avoid
    return item


# ---------------------------------------------------------------- 1-a: visuals の無い台本は変わらない

@pytest.mark.parametrize("name", RECENT_SCRIPTS)
def test_recent_scripts_have_no_visuals(name):
    segments = _load_segments(_raw(REAL_SCRIPTS_DIR / name))
    assert segments
    assert all(seg.visuals is None and seg.scene_visual is None for seg in segments)


@pytest.mark.parametrize("name", RECENT_SCRIPTS)
def test_recent_scripts_split_and_prompt_input_unchanged(mock_app_config, name):
    segments = _load_segments(_raw(REAL_SCRIPTS_DIR / name))
    timeline, asked, timings = _run_timeline(mock_app_config, segments)

    # 変更前と同じ計算で、画像の依頼に渡るはずのセグメントを組み立てる
    expected: list[ScriptSegment] = []
    for seg, timing, entry in zip(segments, timings, timeline.segments):
        starts = split_scene_starts(
            timing.turn_start_secs, timing.end_sec,
            turns=seg.turns, key_claim_source_idxs=seg.key_claim_source_idxs,
        )
        assert [scene.start_turn for scene in entry.scenes] == starts
        if len(starts) == 1:
            expected.append(seg)
            continue
        bounds = starts + [len(seg.turns)]
        for k in range(len(starts)):
            expected.append(seg.model_copy(update={
                "segment_id": f"{seg.segment_id}_scene{k + 1}",
                "turns": seg.turns[bounds[k]:bounds[k + 1]],
            }))
    assert any(len(entry.scenes) > 1 for entry in timeline.segments)  # 分割される章を含むこと
    assert [s.model_dump() for s in asked] == [s.model_dump() for s in expected]

    # LLM に渡る user メッセージは変更前の文面そのまま
    with patch(FACTORY_PATH) as mock_factory_create:
        gen, port = _make_generator(mock_factory_create)
        for seg in asked:
            asyncio.run(gen.generate_prompt(seg))
            user_prompt = port.generate.call_args.args[0].user_prompt
            assert user_prompt == OLD_USER_MESSAGE.format(
                segment_type=seg.segment_type,
                topic=seg.topic_title or "General discussion",
                context=gen._build_segment_context(seg),
            )


def test_null_visuals_is_same_as_no_visuals(caplog):
    raw = _raw()
    for seg in raw["script"]["segments"]:
        seg["visuals"] = None
    with caplog.at_level(logging.WARNING, logger=LOADER_LOGGER):
        segments = _load_segments(raw)
    assert all(seg.visuals is None for seg in segments)
    assert not [r for r in caplog.records if "visuals" in r.getMessage()]


# ---------------------------------------------------------------- visuals がある章

def test_valid_visuals_reach_script_segment():
    raw = _raw()
    dd = _deep_dive_indices(raw)[0]
    raw["script"]["segments"][dd]["visuals"] = [
        _visual(0, "電子レンジの干渉", "黒いルーター 1 台と電子レンジ", "ルーターが床に直置き"),
        _visual(5, "周波数帯", "ルーター 1 台のアンテナ"),
    ]
    segments = _load_segments(raw)
    visuals = segments[dd].visuals
    assert visuals == [
        SceneVisual(start_turn=0, focus="電子レンジの干渉", draw="黒いルーター 1 台と電子レンジ", avoid="ルーターが床に直置き"),
        SceneVisual(start_turn=5, focus="周波数帯", draw="ルーター 1 台のアンテナ"),
    ]
    assert all(seg.visuals is None for k, seg in enumerate(segments) if k != dd)


def test_loader_load_still_reads_file_with_visuals(tmp_path):
    raw = _raw()
    dd = _deep_dive_indices(raw)[0]
    raw["script"]["segments"][dd]["visuals"] = "壊れた値"  # 不正でもファイルは読める
    path = tmp_path / "verified_script.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    script = RadioDirectorScriptLoader().load(path)
    assert len(script.sections) == sum(len(seg["turns"]) for seg in raw["script"]["segments"])


def test_visuals_cut_scenes_at_start_turns(mock_app_config):
    raw = _raw()
    dd1, dd2 = _deep_dive_indices(raw)[:2]
    n1 = len(raw["script"]["segments"][dd1]["turns"])
    cuts = [0, n1 // 3, 2 * n1 // 3]
    raw["script"]["segments"][dd1]["visuals"] = [
        _visual(cut, f"要点{k}", f"描くもの{k}", f"避けるもの{k}" if k else None) for k, cut in enumerate(cuts)
    ]
    # 1 場面だけの指定も効く (章そのものの id のまま、scene_visual が付く)
    raw["script"]["segments"][dd2]["visuals"] = [_visual(0, "全体", "ルーター 2 台")]
    segments = _load_segments(raw)
    timeline, asked, timings = _run_timeline(mock_app_config, segments)

    entry = timeline.segments[dd1]
    assert [scene.start_turn for scene in entry.scenes] == cuts
    assert [scene.end_turn for scene in entry.scenes] == cuts[1:] + [n1]
    # 場面の時刻は今までどおりターンの開始時刻
    for scene in entry.scenes[1:]:
        assert scene.video_start_sec == pytest.approx(timings[dd1].turn_start_secs[scene.start_turn])
    assert sum(sc.video_duration_sec for sc in entry.scenes) == pytest.approx(entry.video_duration_sec)

    by_id = {seg.segment_id: seg for seg in asked}
    seg_id = segments[dd1].segment_id
    for k, scene in enumerate(entry.scenes):
        scene_seg = by_id[f"{seg_id}_scene{k + 1}"]
        assert scene_seg.scene_visual == segments[dd1].visuals[k]
        assert scene_seg.turns == segments[dd1].turns[scene.start_turn:scene.end_turn]

    one = by_id[segments[dd2].segment_id]
    assert one.scene_visual.draw == "ルーター 2 台"
    assert len(timeline.segments[dd2].scenes) == 1


def test_visuals_ignore_turn_count_limit_of_splitter(mock_app_config):
    """visuals は台本側の指定なので、場面の数は章の長さ (90 秒ごと) に依らない。"""
    raw = _raw()
    intro = 0
    n = len(raw["script"]["segments"][intro]["turns"])
    assert n >= 2
    raw["script"]["segments"][intro]["visuals"] = [_visual(0), _visual(1)]
    segments = _load_segments(raw)
    timeline, *_ = _run_timeline(mock_app_config, segments)
    assert [scene.start_turn for scene in timeline.segments[intro].scenes] == [0, 1]


def test_visuals_fall_back_when_turn_times_are_missing(mock_app_config, caplog):
    raw = _raw()
    dd = _deep_dive_indices(raw)[0]
    n = len(raw["script"]["segments"][dd]["turns"])
    raw["script"]["segments"][dd]["visuals"] = [_visual(0), _visual(n // 2)]
    segments = _load_segments(raw)
    timings, _ = _timings(segments)
    timings[dd].turn_start_secs = []  # 推定タイミング (ターンの時刻が無い)
    with caplog.at_level(logging.WARNING):
        timeline, asked, _ = _run_timeline(mock_app_config, segments, timings)
    assert [scene.start_turn for scene in timeline.segments[dd].scenes] == [0]
    assert all(seg.scene_visual is None for seg in asked)
    assert any("falling back to scene_splitter" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- 1-b: 不正な visuals はその章だけ戻る

def _invalid_cases(num_turns: int) -> dict[str, object]:
    return {
        "out_of_range": [_visual(0), _visual(num_turns)],
        "negative": [_visual(0), _visual(-1)],
        "descending": [_visual(0), _visual(6), _visual(3)],
        "duplicate_start": [_visual(0), _visual(3), _visual(3)],
        "first_not_zero": [_visual(2), _visual(5)],
        "zero_scenes": [],
        "five_scenes": [_visual(k) for k in range(5)],
        "empty_draw": [_visual(0), _visual(3, draw="")],
        "blank_draw": [_visual(0), _visual(3, draw="  ")],
        "missing_draw": [{"start_turn": 0, "focus": "要点"}],
        "missing_focus": [{"start_turn": 0, "draw": "描くもの"}],
        "missing_start_turn": [{"focus": "要点", "draw": "描くもの"}],
        "start_turn_as_string": [_visual(0), {"start_turn": "3", "focus": "要点", "draw": "描くもの"}],
        "start_turn_as_bool": [{"start_turn": False, "focus": "要点", "draw": "描くもの"}],
        "start_turn_as_float": [_visual(0), {"start_turn": 3.0, "focus": "要点", "draw": "描くもの"}],
        "draw_not_string": [_visual(0, draw=["ルーター"])],
        "avoid_not_string": [_visual(0, avoid=1)],
        "item_not_object": [_visual(0), "ルーター"],
        "not_a_list": {"start_turn": 0, "focus": "要点", "draw": "描くもの"},
    }


@pytest.mark.parametrize("case", list(_invalid_cases(20)))
def test_invalid_visuals_fall_back_only_for_that_segment(mock_app_config, caplog, case):
    raw = _raw()
    dd1, dd2 = _deep_dive_indices(raw)[:2]
    n1 = len(raw["script"]["segments"][dd1]["turns"])
    n2 = len(raw["script"]["segments"][dd2]["turns"])
    assert n2 > 6
    good = [_visual(0, "前半", "ルーター 1 台"), _visual(n1 // 2, "後半", "ルーター 2 台", "床に直置き")]
    raw["script"]["segments"][dd1]["visuals"] = good
    raw["script"]["segments"][dd2]["visuals"] = _invalid_cases(n2)[case]

    with caplog.at_level(logging.WARNING, logger=LOADER_LOGGER):
        segments = _load_segments(raw)
    assert segments[dd2].visuals is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert segments[dd2].segment_id in warnings[0] and "scene_splitter" in warnings[0]
    assert segments[dd1].visuals is not None

    timeline, asked, timings = _run_timeline(mock_app_config, segments)
    # 不正な章は scene_splitter のまま
    t2 = timings[dd2]
    assert [scene.start_turn for scene in timeline.segments[dd2].scenes] == split_scene_starts(
        t2.turn_start_secs, t2.end_sec,
        turns=segments[dd2].turns, key_claim_source_idxs=segments[dd2].key_claim_source_idxs,
    )
    dd2_id = segments[dd2].segment_id
    assert all(
        seg.scene_visual is None for seg in asked
        if seg.segment_id == dd2_id or seg.segment_id.startswith(f"{dd2_id}_scene")
    )
    # ほかの章は visuals どおり
    assert [scene.start_turn for scene in timeline.segments[dd1].scenes] == [0, n1 // 2]
    dd1_id = segments[dd1].segment_id
    assert [seg.scene_visual.draw for seg in asked if seg.segment_id.startswith(f"{dd1_id}_scene")] == [
        "ルーター 1 台", "ルーター 2 台",
    ]


def test_parse_accepts_four_scenes_and_missing_or_null_avoid():
    raw = [_visual(0), _visual(2, avoid="床置き"), {**_visual(4), "avoid": None}, _visual(7)]
    visuals = parse_scene_visuals(raw, 8, "deep_dive_1")
    assert [v.start_turn for v in visuals] == [0, 2, 4, 7]
    assert [v.avoid for v in visuals] == [None, "床置き", None, None]


def test_parse_returns_none_for_none_without_warning(caplog):
    with caplog.at_level(logging.WARNING, logger=LOADER_LOGGER):
        assert parse_scene_visuals(None, 8, "intro") is None
    assert not caplog.records


# ---------------------------------------------------------------- プロンプト

def _scene_segment(avoid="ルーターを床に直置きする") -> ScriptSegment:
    return ScriptSegment(
        segment_id="deep_dive_3_scene2",
        segment_type="deep_dive",
        topic_title="置き場所で変わる電波",
        turns=[
            {"speaker": "A", "text": "ルーターは床への直置きは避けて、棚の上に置くのがいいのだ。[src=3]"},
            {"speaker": "B", "text": "高さがあると電波が部屋に広がりやすいのよ。"},
        ],
        scene_visual=SceneVisual(
            start_turn=4,
            focus="置き場所は高く、部屋の中央寄りに",
            draw="白いルーター 1 台、腰の高さの木の棚の上、部屋の中央寄り",
            avoid=avoid,
        ),
    )


@patch(FACTORY_PATH)
def test_scene_visual_goes_into_user_message(mock_factory_create):
    gen, port = _make_generator(mock_factory_create)
    seg = _scene_segment()
    prompt = asyncio.run(gen.generate_prompt(seg))

    request = port.generate.call_args.args[0]
    user_prompt = request.user_prompt
    assert "- Point of this scene: 置き場所は高く、部屋の中央寄りに" in user_prompt
    assert "- Draw: 白いルーター 1 台、腰の高さの木の棚の上、部屋の中央寄り" in user_prompt
    assert "- The picture must NOT show: ルーターを床に直置きする" in user_prompt
    # avoid は逆の状態を肯定形で描かせ、避ける状態を書かせない
    assert "opposite state" in user_prompt and "not even negated" in user_prompt
    # 台数を英語で書かせる
    assert "NUMBER of units" in user_prompt and "English word" in user_prompt
    # 場面の本文も渡る
    assert "棚の上に置く" in user_prompt
    # 番号は 1 から順に振られている
    numbers = [line.split(".")[0] for line in user_prompt.split("INSTRUCTIONS:\n")[1].split("\n\n")[0].split("\n")]
    assert numbers == [str(k) for k in range(1, len(numbers) + 1)]
    # avoid はネガティブプロンプトに入らない (このクラスはネガティブプロンプトを作らない)。前置きは変わらない
    assert prompt == gen.SEGMENT_PROMPT_PREFIX + "Two small white routers on a wooden shelf"
    assert "ルーターを床に直置きする" not in request.system_prompt


@patch(FACTORY_PATH)
def test_scene_visual_without_avoid_has_no_avoid_lines(mock_factory_create):
    gen, port = _make_generator(mock_factory_create)
    asyncio.run(gen.generate_prompt(_scene_segment(avoid=None)))
    user_prompt = port.generate.call_args.args[0].user_prompt
    assert "must NOT show" not in user_prompt
    assert "opposite state" not in user_prompt
    numbers = [line.split(".")[0] for line in user_prompt.split("INSTRUCTIONS:\n")[1].split("\n\n")[0].split("\n")]
    assert numbers == ["1", "2", "3", "4", "5"]


@patch(FACTORY_PATH)
def test_scene_visual_system_prompt_is_unchanged(mock_factory_create):
    gen, port = _make_generator(mock_factory_create)
    seg = _scene_segment()
    asyncio.run(gen.generate_prompt(seg))
    with_visual = port.generate.call_args.args[0].system_prompt
    asyncio.run(gen.generate_prompt(seg.model_copy(update={"scene_visual": None})))
    without_visual = port.generate.call_args.args[0].system_prompt
    assert with_visual == without_visual
