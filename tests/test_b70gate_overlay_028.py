"""ブリーフ 028: 受付窓口(b70-gate)向けの overlay。

config/b70gate.overlay.yaml が読めて、comfyui.base_url 以外は config/b70.overlay.yaml と同じであること。
窓口・B70 へは接続しない(ファイルと load_config だけ)。
"""
from pathlib import Path

import yaml

from core.models.config import load_config

ROOT = Path(__file__).resolve().parent.parent
B70_OVERLAY = ROOT / "config" / "b70.overlay.yaml"
GATE_OVERLAY = ROOT / "config" / "b70gate.overlay.yaml"
GATE_URL = "http://192.168.0.86:8188"


def test_b70gate_overlay_differs_from_b70_only_in_base_url(monkeypatch):
    b70 = yaml.safe_load(B70_OVERLAY.read_text(encoding="utf-8"))
    gate = yaml.safe_load(GATE_OVERLAY.read_text(encoding="utf-8"))

    # ファイルとして: base_url だけ違い、他のキー・値はすべて同じ
    assert gate["comfyui"]["base_url"] == GATE_URL
    assert b70["comfyui"]["base_url"] != GATE_URL
    gate_rest = {**gate, "comfyui": {k: v for k, v in gate["comfyui"].items() if k != "base_url"}}
    b70_rest = {**b70, "comfyui": {k: v for k, v in b70["comfyui"].items() if k != "base_url"}}
    assert gate_rest == b70_rest

    # 設定として: 重ねて読めて、base_url 以外は b70 を重ねたときと同じ
    monkeypatch.delenv("ARG_CONFIG", raising=False)
    via_b70 = load_config(ROOT, overlay="config/b70.overlay.yaml")
    via_gate = load_config(ROOT, overlay="config/b70gate.overlay.yaml")
    assert via_gate.yaml.comfyui.base_url == GATE_URL
    gate_dump = via_gate.yaml.model_dump()
    b70_dump = via_b70.yaml.model_dump()
    gate_dump["comfyui"]["base_url"] = b70_dump["comfyui"]["base_url"]
    assert gate_dump == b70_dump

    # 渡さなければ読まれない(既定は今までどおり)
    plain = load_config(ROOT)
    assert plain.yaml.comfyui.base_url == "http://127.0.0.1:8188"
