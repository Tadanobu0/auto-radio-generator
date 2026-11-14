"""ブリーフ 022: 画像段を B70 の ComfyUI へ送る(ARG 側)。

- ComfyUIClient: ノードを決め打ちの番号か class_type と配線で探す / sampler・scheduler・steps・cfg・
  大きさは config から / /view の subfolder・type は history の値 / ポーリング間隔は設定(既定 2 秒)
- B70 用のワークフロー JSON(config/workflow_b70_flux1_schnell_fp8.api.json)と設定の例(config/b70.overlay.yaml)
- config の切り替え口: ARG_CONFIG / load_config(overlay=) / run_workflow_sync(config_overlay=)
- 画像のキャッシュのキーに種類・送り先・大きさを入れる(localhost だけ前の形も見る)

httpx は MockTransport で差し替える。B70・Windows には接続しない。
"""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from core.models.config import ComfyUIConfig, deep_merge, load_config
from services.media_processing import comfyui_client as comfyui_module
from services.media_processing.comfyui_client import ComfyUIClient
from services.media_processing.image_provider import ImageProvider

ROOT = Path(__file__).resolve().parent.parent
B70_WORKFLOW = ROOT / "config" / "workflow_b70_flux1_schnell_fp8.api.json"
LEGACY_WORKFLOW = ROOT / "config" / "workflow_api.json"
B70_OVERLAY = ROOT / "config" / "b70.overlay.yaml"
B70_URL = "http://192.168.0.70:8188"


def _b70_config(**changes) -> SimpleNamespace:
    values = dict(
        base_url=B70_URL,
        workflow_path="config/workflow_b70_flux1_schnell_fp8.api.json",
        timeout=60,
        steps=4,
        width=768,
        height=432,
        cfg=1.0,
        sampler_name="euler",
        scheduler="normal",  # b70.overlay.yaml と同じ(023 で本番に合わせて simple → normal)
        poll_interval_sec=0.5,
    )
    values.update(changes)
    return SimpleNamespace(yaml=SimpleNamespace(comfyui=ComfyUIConfig(**values)))


class _FakeComfy:
    """POST /prompt → GET /history/{id}(最初は空、pending 回のあと完了)→ GET /view。"""

    def __init__(self, pending=1, outputs=None, status=None, view_bytes=b"PNG-BYTES"):
        self.pending = pending
        self.outputs = outputs if outputs is not None else {
            "9": {"images": [{"filename": "flux1-schnell_00001_.png", "subfolder": "ARG", "type": "output"}]}
        }
        self.status = status or {"status_str": "success", "completed": True, "messages": []}
        self.view_bytes = view_bytes
        self.submitted = []
        self.history_calls = 0
        self.view_params = []
        self.hosts = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.hosts.add(f"{request.url.scheme}://{request.url.host}:{request.url.port}")
        path = request.url.path
        if request.method == "POST" and path == "/prompt":
            self.submitted.append(json.loads(request.content))
            return httpx.Response(200, json={"prompt_id": "p1", "number": 0})
        if path == "/history/p1":
            self.history_calls += 1
            if self.history_calls <= self.pending:
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"p1": {"status": self.status, "outputs": self.outputs}})
        if path == "/view":
            self.view_params.append(dict(request.url.params))
            return httpx.Response(200, content=self.view_bytes)
        return httpx.Response(404)


@pytest.fixture
def fake_comfy(monkeypatch):
    """ComfyUIClient の httpx.AsyncClient を MockTransport に差し替え、sleep は記録だけにする。"""
    server = _FakeComfy()
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(server.handler)
        return real_client(*args, **kwargs)

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(comfyui_module.httpx, "AsyncClient", client_factory)
    # comfyui_client が見る asyncio だけを差し替える(他のモジュールの sleep は数えない)
    monkeypatch.setattr(comfyui_module, "asyncio", SimpleNamespace(sleep=fake_sleep))
    server.sleeps = sleeps
    return server


# ---------------------------------------------------------------- ワークフロー JSON

def test_b70_workflow_has_the_shape_pm_confirmed():
    wf = json.loads(B70_WORKFLOW.read_text(encoding="utf-8"))
    assert {k: v["class_type"] for k, v in wf.items()} == {
        "3": "KSampler",
        "4": "CheckpointLoaderSimple",
        "5": "EmptySD3LatentImage",
        "6": "CLIPTextEncode",
        "7": "CLIPTextEncode",
        "8": "VAEDecode",
        "9": "SaveImage",
    }
    k = wf["3"]["inputs"]
    assert (k["model"], k["positive"], k["negative"], k["latent_image"]) == (["4", 0], ["6", 0], ["7", 0], ["5", 0])
    assert (k["sampler_name"], k["scheduler"], k["steps"], k["cfg"], k["denoise"]) == ("euler", "simple", 4, 1.0, 1.0)
    assert wf["4"]["inputs"]["ckpt_name"] == "flux1-schnell-fp8.safetensors"
    assert wf["5"]["inputs"]["batch_size"] == 1
    assert wf["6"]["inputs"]["clip"] == ["4", 1] and wf["7"]["inputs"]["clip"] == ["4", 1]
    assert wf["7"]["inputs"]["text"] == ""
    assert wf["8"]["inputs"] == {"samples": ["3", 0], "vae": ["4", 2]}
    assert wf["9"]["inputs"]["images"] == ["8", 0]


def test_existing_workflow_api_json_is_unchanged():
    wf = json.loads(LEGACY_WORKFLOW.read_text(encoding="utf-8"))
    assert wf["5"]["class_type"] == "EmptyLatentImage"
    assert wf["7"]["inputs"]["text"] == "text, watermark"
    assert wf["9"]["inputs"]["filename_prefix"] == "SD1.5"


# ---------------------------------------------------------------- ノードの探し方

def _client(config=None) -> ComfyUIClient:
    return ComfyUIClient(config or _b70_config())


def test_fixed_ids_are_used_for_both_workflows():
    expected = {"ksampler": "3", "empty_latent": "5", "clip_text_pos": "6", "clip_text_neg": "7", "save_image": "9"}
    client = _client()
    assert client.resolve_nodes(json.loads(B70_WORKFLOW.read_text(encoding="utf-8"))) == expected
    assert client.resolve_nodes(json.loads(LEGACY_WORKFLOW.read_text(encoding="utf-8"))) == expected


def _renumbered(workflow: dict, mapping: dict) -> dict:
    """番号を付け替えたワークフロー(配線も付け替える)。"""
    out = {}
    for old, node in workflow.items():
        node = copy.deepcopy(node)
        for name, value in node["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and value[0] in mapping:
                node["inputs"][name] = [mapping[value[0]], value[1]]
        out[mapping[old]] = node
    return out


def test_nodes_are_found_by_class_type_and_links_when_ids_differ():
    wf = json.loads(B70_WORKFLOW.read_text(encoding="utf-8"))
    # 正と負を入れ替え、番号もずらす(決め打ちの 3/5/6/7/9 は別の役のノードになる)
    mapping = {"3": "12", "4": "3", "5": "9", "6": "7", "7": "6", "8": "5", "9": "20"}
    moved = _renumbered(wf, mapping)
    nodes = _client().resolve_nodes(moved)
    assert nodes == {"ksampler": "12", "empty_latent": "9", "clip_text_pos": "7", "clip_text_neg": "6", "save_image": "20"}


def test_node_ids_from_config_override_the_fixed_ones():
    wf = _renumbered(
        json.loads(B70_WORKFLOW.read_text(encoding="utf-8")),
        {"3": "30", "4": "40", "5": "50", "6": "60", "7": "70", "8": "80", "9": "90"},
    )
    client = _client(_b70_config(node_ids={"ksampler": "30", "save_image": "90"}))
    assert client.node_ids["ksampler"] == "30" and client.node_ids["checkpoint"] == "4"
    assert client.resolve_nodes(wf)["save_image"] == "90"


def test_missing_ksampler_is_a_runtime_error():
    wf = json.loads(B70_WORKFLOW.read_text(encoding="utf-8"))
    del wf["3"]
    with pytest.raises(RuntimeError, match="no KSampler"):
        _client().resolve_nodes(wf)


def test_latent_node_of_another_class_is_a_runtime_error():
    wf = json.loads(B70_WORKFLOW.read_text(encoding="utf-8"))
    wf["5"]["class_type"] = "LoadImage"
    with pytest.raises(RuntimeError, match="latent_image"):
        _client().resolve_nodes(wf)


# ---------------------------------------------------------------- generate_image(httpx のモック)

def test_generate_image_against_b70(fake_comfy, tmp_path):
    client = _client(_b70_config(width=1344, height=768))
    out = tmp_path / "cache" / "x.png"
    path, metadata = asyncio.run(client.generate_image(
        prompt="A humidifier on a shelf", output_path=out, context_type="segment",
        segment_id="deep_dive_1_scene1", segment_type="deep_dive", visual_identity={}, seed=1234,
    ))
    assert fake_comfy.hosts == {B70_URL}
    sent = fake_comfy.submitted[0]["prompt"]
    assert sent["6"]["inputs"]["text"] == "A humidifier on a shelf"
    assert sent["7"]["inputs"]["text"] == ""  # 負は書き換えない
    assert (sent["5"]["inputs"]["width"], sent["5"]["inputs"]["height"]) == (1344, 768)
    k = sent["3"]["inputs"]
    # 写しの JSON は simple のまま。送る中身は config の normal に書き換わる(023)
    assert (k["seed"], k["steps"], k["cfg"], k["sampler_name"], k["scheduler"]) == (1234, 4, 1.0, "euler", "normal")
    # 取り出しは history の subfolder / type
    assert fake_comfy.view_params == [{"filename": "flux1-schnell_00001_.png", "subfolder": "ARG", "type": "output"}]
    assert path == out and out.read_bytes() == b"PNG-BYTES"
    assert metadata.seed == 1234 and metadata.resolution == "1344x768"
    # 0.5 秒刻みで 2 回見た(1 回目は history がまだ無い)
    assert fake_comfy.sleeps == [0.5, 0.5]
    # 読み込んだワークフローは書き換えない
    assert client.workflow["6"]["inputs"]["text"] == ""


def test_sender_is_in_extra_data(fake_comfy, tmp_path):
    # 026: 共有の ComfyUI の列・history で送り主が分かるように
    asyncio.run(_client().generate_image(prompt="p", output_path=tmp_path / "a.png"))
    body = fake_comfy.submitted[0]
    assert body["extra_data"] == {"sender": "radio"}
    assert set(body) == {"prompt", "extra_data"}


def test_random_seed_when_not_given(fake_comfy, tmp_path):
    _, metadata = asyncio.run(_client().generate_image(prompt="p", output_path=tmp_path / "a.png"))
    seed = fake_comfy.submitted[0]["prompt"]["3"]["inputs"]["seed"]
    assert 1 <= seed <= 10**15 and metadata.seed == seed


def test_execution_error_stops_with_the_message(fake_comfy, tmp_path):
    fake_comfy.pending = 0
    fake_comfy.status = {
        "status_str": "error", "completed": False,
        "messages": [["execution_error", {"node_id": "4", "node_type": "CheckpointLoaderSimple",
                                          "exception_type": "FileNotFoundError", "exception_message": "no ckpt"}]],
    }
    with pytest.raises(RuntimeError, match="node 4 \\(CheckpointLoaderSimple\\): FileNotFoundError no ckpt"):
        asyncio.run(_client().generate_image(prompt="p", output_path=tmp_path / "a.png"))
    assert fake_comfy.history_calls == 1  # タイムアウトまで待たない
    assert fake_comfy.view_params == []


def test_output_without_subfolder_keeps_the_old_request(fake_comfy, tmp_path):
    fake_comfy.outputs = {"9": {"images": [{"filename": "SD1.5_00001_.png"}]}}
    config = _b70_config(base_url="http://127.0.0.1:8188", workflow_path="config/workflow_api.json")
    asyncio.run(_client(config).generate_image(prompt="p", output_path=tmp_path / "a.png"))
    assert fake_comfy.view_params == [{"filename": "SD1.5_00001_.png", "subfolder": "", "type": "output"}]


def test_timeout_counts_polls_by_the_interval(fake_comfy, tmp_path):
    fake_comfy.pending = 10**6
    with pytest.raises(RuntimeError, match="timeout"):
        asyncio.run(_client(_b70_config(timeout=3, poll_interval_sec=0.5)).generate_image(
            prompt="p", output_path=tmp_path / "a.png"))
    assert fake_comfy.sleeps == [0.5] * 6


def test_default_poll_interval_is_two_seconds():
    assert ComfyUIConfig().poll_interval_sec == 2.0
    assert ComfyUIConfig().node_ids is None
    client = ComfyUIClient(SimpleNamespace(yaml=SimpleNamespace(comfyui=ComfyUIConfig())))
    assert client.poll_interval_sec == 2.0
    assert (client.sampler_name, client.scheduler) == ("euler", "normal")
    assert client.node_ids == ComfyUIClient.NODE_IDS


def test_bad_poll_interval_falls_back_to_two_seconds():
    client = _client(_b70_config(poll_interval_sec=0))
    assert client.poll_interval_sec == 2.0


def test_cache_identities():
    from services.media_processing.flux_client import FluxClient

    assert _client(_b70_config(width=1344, height=768)).cache_identity() == f"comfyui|{B70_URL}|1344x768"
    flux = FluxClient(SimpleNamespace(yaml=SimpleNamespace(flux=None)))
    assert flux.cache_identity() == "forge|http://127.0.0.1:7890|1344x768"


# ---------------------------------------------------------------- config の切り替え口

def test_deep_merge():
    base = {"a": 1, "c": {"x": 1, "y": 2}, "l": [1, 2]}
    assert deep_merge(base, {"c": {"y": 3, "z": 4}, "l": [9], "n": None}) == {
        "a": 1, "c": {"x": 1, "y": 3, "z": 4}, "l": [9], "n": None,
    }
    assert base == {"a": 1, "c": {"x": 1, "y": 2}, "l": [1, 2]}  # 元は変えない


def test_no_overlay_is_as_before(monkeypatch):
    monkeypatch.delenv("ARG_CONFIG", raising=False)
    config = load_config(ROOT)
    assert config.yaml.image_provider == "comfyui"
    assert config.yaml.comfyui.base_url == "http://127.0.0.1:8188"
    assert config.yaml.comfyui.workflow_path == "config/workflow_api.json"
    assert config.yaml.comfyui.poll_interval_sec == 2.0


def test_b70_overlay_from_env(monkeypatch):
    monkeypatch.delenv("ARG_CONFIG", raising=False)
    plain = load_config(ROOT)
    monkeypatch.setenv("ARG_CONFIG", "config/b70.overlay.yaml")
    config = load_config(ROOT)
    c = config.yaml.comfyui
    assert config.yaml.image_provider == "comfyui"
    assert c.base_url == B70_URL
    assert c.workflow_path == "config/workflow_b70_flux1_schnell_fp8.api.json"
    assert (c.sampler_name, c.scheduler, c.steps, c.cfg, c.poll_interval_sec) == ("euler", "normal", 4, 1.0, 0.5)
    # 大きさは本番(config.yaml)と同じ値の例
    assert (c.width, c.height) == (plain.yaml.comfyui.width, plain.yaml.comfyui.height)
    # 重ねていない節はそのまま
    assert config.yaml.video_renderer == plain.yaml.video_renderer
    assert config.yaml.flux == plain.yaml.flux
    assert config.yaml.script_generator == plain.yaml.script_generator
    # B70 の設定で ComfyUIClient が作れる(ワークフローの写しが読める)
    client = ComfyUIClient(config)
    assert client.base_url == B70_URL and client.poll_interval_sec == 0.5


def test_overlay_argument_wins_over_env(monkeypatch, tmp_path):
    other = tmp_path / "other.yaml"
    other.write_text("comfyui:\n  base_url: http://10.0.0.9:8188\n", encoding="utf-8")
    monkeypatch.setenv("ARG_CONFIG", str(B70_OVERLAY))
    config = load_config(ROOT, overlay=other)
    assert config.yaml.comfyui.base_url == "http://10.0.0.9:8188"
    assert config.yaml.comfyui.workflow_path == "config/workflow_api.json"  # other.yaml に無いキーは config.yaml のまま


def test_missing_overlay_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("ARG_CONFIG", str(tmp_path / "nope.yaml"))
    with pytest.raises(FileNotFoundError, match="ARG_CONFIG"):
        load_config(ROOT)


def test_broken_overlay_is_an_error(monkeypatch, tmp_path):
    monkeypatch.delenv("ARG_CONFIG", raising=False)
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_config(ROOT, overlay=bad)
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("image_provider: dalle\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(ROOT, overlay=invalid)


def test_run_workflow_sync_passes_config_overlay(monkeypatch):
    import workflow

    monkeypatch.delenv("ARG_CONFIG", raising=False)
    calls = []

    def fake_load_config(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("stop here")

    monkeypatch.setattr(workflow, "load_config", fake_load_config)
    result = workflow.run_workflow_sync(theme="t", config_overlay="config/b70.overlay.yaml")
    assert result.success is False
    assert calls == [((workflow.PROJECT_ROOT,), {"overlay": "config/b70.overlay.yaml"})]

    calls.clear()
    workflow.run_workflow_sync(theme="t")
    assert calls == [((workflow.PROJECT_ROOT,), {})]  # 指定が無ければ今までどおりの呼び方


# ---------------------------------------------------------------- キャッシュのキー

def _provider(tmp_path, monkeypatch, client) -> ImageProvider:
    monkeypatch.chdir(tmp_path)  # output/.image_cache を tmp に作る
    config = SimpleNamespace(yaml=SimpleNamespace(video_renderer=SimpleNamespace(background_mode="static")))
    provider = ImageProvider(config, output_dir=tmp_path / "run")
    provider.image_client = client
    return provider


def _legacy_name(prompt: str, suffix: str = "") -> str:
    return hashlib.sha256(prompt.encode()).hexdigest() + suffix + ".png"


def test_cache_key_has_backend_and_size(tmp_path, monkeypatch):
    b70 = SimpleNamespace(base_url=B70_URL, cache_identity=lambda: f"comfyui|{B70_URL}|768x432")
    big = SimpleNamespace(base_url=B70_URL, cache_identity=lambda: f"comfyui|{B70_URL}|1344x768")
    local = SimpleNamespace(base_url="http://127.0.0.1:8188", cache_identity=lambda: "comfyui|http://127.0.0.1:8188|768x432")
    keys = {_provider(tmp_path, monkeypatch, c)._get_prompt_cache_key("p") for c in (b70, big, local)}
    assert len(keys) == 3
    assert hashlib.sha256(b"p").hexdigest() not in keys


def test_client_without_identity_keeps_the_old_key(tmp_path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch, SimpleNamespace(generate_image=None))
    assert provider._get_prompt_cache_key("p") == hashlib.sha256(b"p").hexdigest()
    assert provider._cache_path("p").name == _legacy_name("p")


def test_local_backend_still_hits_old_style_files(tmp_path, monkeypatch):
    local = SimpleNamespace(base_url="http://127.0.0.1:8188", cache_identity=lambda: "comfyui|http://127.0.0.1:8188|768x432")
    provider = _provider(tmp_path, monkeypatch, local)
    legacy = provider.cache_dir / _legacy_name("p")
    legacy_seed = provider.cache_dir / _legacy_name("p", "_seed7")
    legacy.write_bytes(b"old")
    legacy_seed.write_bytes(b"old7")
    assert provider._cache_path("p") == legacy
    assert provider._cache_path("p", "_seed7") == legacy_seed
    # 作り直し(allow_legacy=False)は新しい形のキーに書く
    assert provider._cache_path("p", "_seed7", allow_legacy=False).name != legacy_seed.name
    # 新しい形があればそちらが先
    new = provider.cache_dir / f"{provider._get_prompt_cache_key('p')}.png"
    new.write_bytes(b"new")
    assert provider._cache_path("p") == new


def test_remote_backend_never_takes_old_style_files(tmp_path, monkeypatch):
    b70 = SimpleNamespace(base_url=B70_URL, cache_identity=lambda: f"comfyui|{B70_URL}|768x432")
    provider = _provider(tmp_path, monkeypatch, b70)
    (provider.cache_dir / _legacy_name("p")).write_bytes(b"old")
    path = provider._cache_path("p")
    assert path.name == f"{provider._get_prompt_cache_key('p')}.png" and not path.exists()


def test_generated_scene_goes_into_the_new_key(tmp_path, monkeypatch):
    """B70 の送り先で作った場面は、種類・送り先・大きさ入りのキーに置かれ、2 回目はキャッシュが当たる。"""
    from core.models.curation import ScriptSegment
    from core.models.generation_metadata import GenerationMetadata

    calls = []

    async def generate_image(prompt, output_path, **kwargs):
        calls.append(output_path)
        output_path.write_bytes(b"img")
        return output_path, GenerationMetadata.create_from_segment(
            image_path=str(output_path), segment_id=kwargs["segment_id"], segment_type=kwargs["segment_type"],
            prompt=prompt, visual_identity={}, seed=5, generation_time_sec=1.0,
            resolution="768x432", steps=4, sampler="comfyui", cfg_scale=1.0,
        )

    client = SimpleNamespace(
        base_url=B70_URL, cache_identity=lambda: f"comfyui|{B70_URL}|768x432", generate_image=generate_image,
    )
    provider = _provider(tmp_path, monkeypatch, client)
    provider.mode = "dynamic"

    async def generate_prompt(segment, visual_identity=None):
        return "A shelf"

    provider.prompt_generator = SimpleNamespace(generate_prompt=generate_prompt, warnings=[])
    seg = ScriptSegment(segment_id="intro", segment_type="intro", topic_title=None, turns=[{"speaker": "A", "text": "x"}])
    first = asyncio.run(provider.get_image_for_segment(seg))
    second = asyncio.run(provider.get_image_for_segment(seg))
    expected = hashlib.sha256(f"comfyui|{B70_URL}|768x432\nA shelf".encode()).hexdigest() + ".png"
    assert first.name == expected and second == first
    assert len(calls) == 1
    assert [r["source"] for r in provider.scene_records] == ["generated", "cache"]
