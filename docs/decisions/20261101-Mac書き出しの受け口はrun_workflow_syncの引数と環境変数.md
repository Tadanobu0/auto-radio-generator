---
project: auto_radio_generator
date: 2026-11-01
---
# Mac 書き出しの 4 つの差し替えを run_workflow_sync の引数(と環境変数)で受ける

- **選択肢**: run_workflow_sync の引数 + 環境変数(012 の skip_upload / ARG_NO_UPLOAD と同じ形)/ config.yaml / 環境変数だけ(017 の形)
- **採用**: 引数 `reuse_strict` / `thumbnail_bg` / `youtube_dir` / `reading_fixes`。引数が無いときだけ `ARG_REUSE_STRICT` / `ARG_THUMBNAIL_BG` / `ARG_YOUTUBE_DIR` / `ARG_READING_FIXES`
- **理由**: run_remote.py は run_workflow_sync を直接呼ぶので、引数なら名前が変わると TypeError で気づける(差し替えのように黙って効かなくならない)
- **捨てた理由**: config.yaml は Windows と Mac で値を分けにくい / 環境変数だけだと綴りの誤りで黙って既定に戻る
- **注意点・未解決**: reuse_strict の事前確認は、visuals の無い章では文字数からの見積もり。実際の音声で場面が増えたら、合成の後(画像を作る前)に止まる
