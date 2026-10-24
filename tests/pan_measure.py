"""パンの動きを数字で測る道具 (010 のテストと試しエンコードで共用)。

黒っぽい地に白いぼかし点を 1 つ置いた 768x432 の画像 (FLUX の出力と同じ大きさ) を用意し、
映像のコマごとに点の重心 (明るさで重み付け) を小数ピクセルで求める。点は画像の中央にあり、
perspective 方式では「画像の中央が出力のどこに来るか」がそのまま測れる。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

CARD_W, CARD_H = 768, 432
CARD_BG = 40
BLOB_SIGMA = 4.0


def write_blob_card(path: Path) -> Path:
    import numpy as np
    from PIL import Image

    yy, xx = np.mgrid[0:CARD_H, 0:CARD_W]
    cx, cy = (CARD_W - 1) / 2, (CARD_H - 1) / 2
    blob = np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * BLOB_SIGMA ** 2))
    img = CARD_BG + (230 - CARD_BG) * blob
    rgb = np.repeat(img[:, :, None], 3, axis=2).round().astype("uint8")
    Image.fromarray(rgb, "RGB").save(path)
    return path


def track_blob(cmd: list[str], width: int, height: int) -> list[tuple[float, float]]:
    """cmd (gray の rawvideo を標準出力に出す ffmpeg) のコマごとの点の重心 (x, y)。"""
    import numpy as np

    frame_bytes = width * height
    yy, xx = np.mgrid[0:height, 0:width]
    positions: list[tuple[float, float]] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(height, width).astype(np.float64)
            weight = np.clip(frame - (CARD_BG + 20), 0, None)
            total = weight.sum()
            if total <= 0:
                positions.append((float("nan"), float("nan")))
            else:
                positions.append((float((weight * xx).sum() / total), float((weight * yy).sum() / total)))
    finally:
        proc.stdout.close()
        proc.wait()
    return positions


def step_stats(positions: list[tuple[float, float]]) -> dict:
    """コマごとの横の移動量 (px) の統計。still は動かなかったコマ (|dx| < 0.001px) の数。"""
    import numpy as np

    x = np.array([p[0] for p in positions])
    dx = np.diff(x)
    moving = np.abs(dx) >= 0.001
    return {
        "frames": len(x),
        "total_px": float(x[-1] - x[0]),
        "mean": float(dx.mean()),
        "std": float(dx.std()),
        "min": float(dx.min()),
        "max": float(dx.max()),
        "still": int((~moving).sum()),
        "max_abs": float(np.abs(dx).max()),
    }


def format_stats(name: str, s: dict) -> str:
    return (
        f"{name}: {s['frames']} コマ, 合計 {s['total_px']:+.2f}px, 1 コマあたり 平均 {s['mean']:+.4f} "
        f"標準偏差 {s['std']:.4f} 最小 {s['min']:+.4f} 最大 {s['max']:+.4f} px, "
        f"止まったコマ {s['still']}, 最大の 1 コマ移動 {s['max_abs']:.3f}px"
    )
