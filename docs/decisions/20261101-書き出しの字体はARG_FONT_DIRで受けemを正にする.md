---
project: auto_radio_generator
date: 2026-11-01
---
# 書き出しの字体は ARG_FONT_DIR で受け、ASS の Fontsize は em の px から出す

- **選択肢**: 環境変数 ARG_FONT_DIR / config.yaml に字体名と大きさ / Fontsize を手で 70 に書き換え
- **採用**: ARG_FONT_DIR(無ければ今の Meiryo)。Fontsize = em 70px × (winAscent+winDescent)/unitsPerEm。字体を替えたときだけ字幕を 1 行 1 Dialogue(L2、PM 決定)
- **理由**: 機ごとに切り替えやすく、既定の本番を一切変えない。libass は win の高さで Fontsize を解釈するので em を正にすると字体が替わっても字の大きさが揃う
- **捨てた理由**: config.yaml は両機で別の値が要る / 手書きの 70 は字体ごとに調べ直しになる
- **注意点・未解決**: 指定したのに Regular が無ければ止める。4 か所とも Regular。章タイトルの行送りは 1.5em のまま(箱の高さは字体で変わる)。見比べの静止画はこのジョブでは書き出せず、scripts/render_font_samples.py を PM 側で流す
