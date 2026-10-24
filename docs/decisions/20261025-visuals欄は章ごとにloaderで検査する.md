---
project: auto_radio_generator
date: 2026-10-25
---
# verified_script の visuals 欄は、ファイルの検査から外して loader で章ごとに検査する

- **選択肢**: VerifiedScript のモデルで SceneVisual を厳密に型付け / 欄は形を問わず受け、loader で章ごとに検査 / タイムライン計算の段で検査
- **採用**: `Segment.visuals` は `Optional[Any]` で受け、`build_script_segments` の `parse_scene_visuals` で検査する。正しいものだけ `ScriptSegment.visuals` に入れる
- **理由**: 仕様 §12 では、不正な visuals があってもファイル全体は拒否しない(その章だけ scene_splitter に戻す)。モデルで型付けすると ValidationError でファイルごと落ちる。ターン数がわかる loader なら、範囲の検査もまとめてできる
- **捨てた理由**: モデルで型付けする案は、上のとおり 1 章の不正で回全体が止まる。タイムラインで検査する案は、検査が 2 か所に分かれる
- **avoid の扱い**: ネガティブプロンプトには入れない。LLM への指示で「逆の状態を肯定形で書き、避ける状態は否定形でも書かない」とした
- **注意点・未解決**: visuals がある章は `max_scenes_per_chapter` を見ない(上限は仕様の 4)。ターンの時刻が無い章は警告を出し、scene_splitter に戻す
