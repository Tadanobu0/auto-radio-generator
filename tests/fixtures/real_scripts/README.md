# 実台本 fixtures (字幕レイアウトの不変条件テスト用)

radio_director の `output/` にあった verified_script.json を **無改変** で置いている。
`tests/test_subtitle_invariants_real_scripts.py` が全ターンを字幕化して、安全域・行数上限・
`[src=N]` の除去を確かめる。選定は 2026-10-11、95 本 (重複除去後) から端のケースを 10 本。

| ファイル | 元の場所 (radio_director/output/) | 選んだ理由 |
|---|---|---|
| 20261007_hermesagent_frontier.json | renders/20261007_190720 | 見切れが起きた公開回 (run 20261007_190721)。回帰の基準 |
| 20260512_exo-ai.json | 2026-05-12_01-25_exo-ai | 最長ターン (207 字) |
| 20260706_aiobsidian.json | 2026-07-06_00-05_aiobsidian | 最長文節 (1 文節で 1707px。上限 1744px の間際) |
| 20261006_llmglm_frontier.json | script/2026-10-06_21-24_llmglm | 英数字の比率が最大 (20.3%)、`[src=N]` が最多 (57 個)。/script 経路 |
| 20260720_theme.json | 2026-07-20_07-29_theme | `[src=N][src=M]` の連続が最多 (2 か所) |
| 20260512_deepseek-v4-flash.json | 2026-05-12_01-02_deepseek-v4-flash | `[src=N]` 以外の角括弧が最多 (26 個)。除去しないことの確認 |
| 20260704_ai.json | 2026-07-04_23-17_ai | 句読点なしの連続が最長 (99 字)。読点で割れない場合の経路 |
| 20261011_style-bert-vits2.json | 2026-10-11_18-32_style-bert-vits2 | 最新日の DSV4 経路。文節幅が 2 番目 (1652px) |
| 20260714_hugging-face.json | 2026-07-14_21-33_hugging-face | DSV4 経路で英数字が多い (12.8%)、長い文節 (1627px) |
| 20260816_ai.json | 2026-08-16_23-29_ai | 長いターン (203 字) に `[src=N]` が混ざる |
