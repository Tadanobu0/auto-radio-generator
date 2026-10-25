---
project: auto_radio_generator
date: 2026-10-25
---
# visuals の警告と画像プロンプトの記録を WorkflowResult で返し、記録は run の直下に寄せる

- **選択肢**: 警告は logging のハンドラで拾う / 章・フェーズの結果に載せて運ぶ。記録は videos/ の下のまま / run の直下に寄せる
- **採用**: 警告は loader とタイムラインで捨てたところで文字列にし、`WorkflowResult.warnings` まで運ぶ。記録は run の直下の prompt_report.md / generation_metadata.jsonl(サムネと同じファイル)。パスを WorkflowResult に持たせる
- **理由**: Mac に返るのは result.json だけで、それを書く run_remote.py は radio_director 側にある。記録が 2 か所に分かれていると取り寄せが面倒
- **捨てた理由**: ハンドラは関係ない警告も拾う。videos/ の下のままだとサムネの記録と分かれる
- **注意点・未解決**: result.json に書くのは run_remote.py の改修(radio_director 側)。avoid は仕様どおり List[str] にした(str も受ける)
