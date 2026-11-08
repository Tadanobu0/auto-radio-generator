---
project: auto_radio_generator
date: 2026-11-08
---
# 画像段は Mac の arg-render から B70 の ComfyUI へ送る(案A)

- **選択肢**: 案A Mac で画像段も回し ARG の ComfyUIClient → B70 / 案B Windows の ARG のまま送り先だけ B70 / 案C forge-gen を共通部品として呼ぶ
- **採用**: 案A(PM 判断 2026-11-08 21:07)。大きさは今の本番のまま送り先だけ替える。Windows の画像段は予備(スイッチは radio_director 側)。ワークフロー JSON は ARG に写し、正本は b70-box。forge-gen と ComfyUI を共用し順番待ちは許す
- **理由**: ARG に ComfyUI のクライアントがあり、画像段の機能は generate_image(seed) の上だけで動く。Windows を起こす経路(終了コード 20)が消える
- **捨てた理由**: B は起こす・使用中で止まるが残る / C は 2 部品の版ずれと seed 制御が未確認
- **注意点・未解決**: 1344×768 への変更は別の作業。B70 の JSON は forge-gen の PNG から PM が起こした形で、b70-box の正本と突き合わせていない
