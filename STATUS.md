# STATUS

## 背景を場面ごとに増やし、パンを入れる(ブリーフ 009、006 の「中」)— 2026-10-24 時点
- ブランチ `pm/009` に実装済み。main へは未マージ。効果は merge・deploy 後の次の /render のコマで見る
- 場面: 章の長さ ÷ 90 秒(四捨五入、1〜4 枚)。等分位置に一番近いターンの頭で切り、±2 ターン以内に新しい論点の出典 `[src=N]` の初出があればそこへ寄せる。4〜5 分の章は 3 枚、21 分の回で約 14 枚の見込み
- `SegmentTiming.turn_start_secs`(章内の各ターンの開始時刻)を追加。`SegmentTimelineEntry.scenes` に場面を持たせ、映像トラックは `VideoTimeline.video_scenes()` の順に並べる。章タイトル・チャプター・ジングルは章単位のまま(`timeline.segments` は章のまま)
- 各場面の画像プロンプトはその場面のターンだけから作る(segment_id は `deep_dive_1_scene2` の形。1 場面の章は従来どおり章そのもの)
- パン: 画面の 1.05 倍に拡大 → crop の x を t の式で動かす。場面の長さで画面幅の 5%(1080p で 96px)、場面ごとに左右交互。ズームなし
- 設定(`video_renderer`): `scene_target_sec: 90` / `max_scenes_per_chapter: 4`(1 で従来の章 1 枚)/ `enable_pan: true` / `pan_ratio: 0.05` / `video_track_use_gpu: false`(true で映像トラックを NVENC)
- 試しエンコード(1 分・1080p30・字幕/日付/章タイトルあり、768x432 の素材画像 2 種): libx264 で静止画 3.4〜3.5 秒・3.3〜3.5 MB → パン 3.4〜3.5 秒・3.6〜3.8 MB(サイズ 1.10〜1.11 倍、時間ほぼ同じ)。NVENC はパンで 3.9 秒・2.3〜2.4 MB
- 映像トラックの既定エンコーダは libx264 ultrafast のまま(2026-10-24 PM 判断。NVENC は `video_track_use_gpu` で切り替え可)
- arg-remote `test -q`: 700 passed / 1 skipped(skip は試しエンコード。`-m encode_trial` で 2 passed)

## 概要欄から出典の生 URL を外す(ブリーフ 008)— 2026-10-24 時点
- ブランチ `pm/008` に実装済み。main へは未マージ。概要欄の組み立てはこのリポジトリの `services/publishing/metadata_builder.py`(radio_director 側ではない)
- 参考文献: 「📄 タイトル / 🔗 URL / 空行」の 3 行 → 「📄 タイトル(ドメイン名、公開日)」の 1 行。ドメインは先頭の www. を外す。タイトル無しは「参考文献N(ドメイン)」
- 保険: `build_video_description` の最後と `_upload_to_youtube` のアップロード直前で `strip_urls` を通す(URL は落として続行、ログに件数と URL)。概要文・フッター由来の URL も落ちる
- metadata.txt の「概要欄用テキスト」の参考文献・概要文からも URL を外した(手で貼る前提のため)
- 旧書式(🔗 URL 行)を要求していた既存テスト `test_metadata_description_format.py` は PM 判断で期待値を 1 行構造に更新
- arg-remote `test -q`: 661 passed / 0 failed

## 画像プロンプトの指示文の改善(ブリーフ 007、006 の「小」)— 2026-10-23 時点
- ブランチ `pm/007` に実装済み。main へは未マージ。効果は merge・deploy 後の次の /render のコマで見る
- 指示文の例 1 を「寝室・医療機器」から「台所の鉄瓶」に。例は構造だけ真似させ、物・場所は真似させない旨を明記。構図の指示の医療の項は「健康・生活の話題は、その話題の物そのもの(医療機器にしない)」に
- 小さな機器の表示部は「暗いガラスの窓 / 色の付いた光だけ(数字・文字・アイコンなし)」を許す。旧ルール「電源オフか背面・側面だけ」は外した
- プロンプトに主役の物・大きさ・材質・置き場所・画面に占める大きさ(40〜60%)を必ず書かせる(例 3 本にも書いた)
- LLM に渡す本文: 先頭 800 字 → 各論点(key_claims の出典番号)を最初に引くターンの前後。無ければ 4 等分の位置。合計 800 字。外部台本の loader が `ScriptSegment.key_claim_source_idxs` を埋める
- 前置き: 「An unpopulated, wordless scene with no people, no hands, no faces, no text, signage or labels: 」(screen・lettering を外して短く。no faces は既存テストが要求)
- CO2 の回の実際のプロンプトは未確認(arg-remote から Windows の output を読めない)
- arg-remote `test -q`: 644 passed / 0 failed

## 背景画像を 1 章 1 枚から増やす調査(ブリーフ 006)— 2026-10-23 時点
- 調査のみ(コード変更なし)。詳細は `.pm/reports/006.md`
- 絵と話のずれの主な原因は画像プロンプトの指示文: 例 1 が「枕元のテーブル」、画面の禁止で表示部の無い箱になる、章の先頭 800 字しか見ていない
- ネガティブプロンプト(`text, watermark`)は cfg 1.0 の FLUX schnell では効いていない。画像キャッシュはプロンプトが毎回変わるためほぼ当たらない
- 枚数は Windows 側だけで増やせる(ターンごとの時刻は音声合成の中にある)。パンは `video_track_renderer._build_concat_filter` の変更で入るが、エンコード時間とファイルサイズが増える(要実測)
- 実装案は 小(プロンプトのみ)/ 中(約 90 秒ごとに 1 枚 + パン)/ 大(台本に `segments[].visuals` を足す)
- PM 判断: 小 → 中の順に別ブリーフで進める。大は中の後に判断。パンのサイズは中のブリーフで 1 分の試しエンコードを測ってから決める(2 倍超なら映像トラックを NVENC にする案を併記)

## 章タイトルの折り返しと表示時刻(ブリーフ 005)— 2026-10-23 時点
- ブランチ `pm/005` に実装済み。main へは未マージ。見た目は merge・deploy 後の次の /render のコマで確かめる
- 章タイトル: 章の間ずっと出る(章の頭〜次の章の頭)。1 行に入らない題に「：」「: 」があれば、そこで主題/副題の 2 行に分ける(「：」は 1 行目の末尾)
- 章タイトルの drawtext を expansion=none に。「%」を含む題が描かれていなかった(drawtext の展開記法)
- 背景の切り替え: 次のセグメントの最初の発話(= 章タイトルの切り替わり)に揃えた。以前は境界ごとに 2 + 0.25k 秒早かった
- arg-remote `test -q`: 621 passed / 0 failed / 0 skipped(ffmpeg を呼ぶテストも Windows で実行)

## 字幕と章タイトルの見た目(ブリーフ 004)— 2026-10-23 時点
- ブランチ `pm/004` に実装済み。main へは未マージ。実際の見た目は merge・deploy 後の次の /render で radio_director のコマ(frames/)を見て確かめる
- 字幕: 下に半透明の黒帯(行数に合わせた高さ、上端をぼかす)。めたんの縁はピンク → 暗い赤紫(&H00502080)。文字色(ずんだもん緑 / めたん白)はそのまま
- 分割: 1 枚 2 行まで。「。」優先、「、」は 1 文が 2 行を超えるときだけ。「、」で分けた残りが 10 字未満にならないよう選ぶ。「…。」と説明…」の括弧の後は文末扱いしない
- 改行: 行頭に助詞・いう/もの/して/こと を置かない(BudouX の文節を前に繋げる)。「の」の直後の改行は行幅の釣り合いを 15% まで崩して避ける
- 章タイトル: 「...」で切らない。Meiryo(字幕と同じ)で 2 行まで折り返し、収まらなければ 80→72→64→56px に縮める
- arg-remote `test -q`: 598 passed / 0 failed / 0 skipped(Python 3.10.6)

## arg-remote(Windows 実機でのテスト経路)— 2026-10-23 時点
- `status` → `sync` → `test` → `status` が1周通った(ブリーフ 003)
- 試験 clone: `E:\windsurf\arg-test\auto_radio_generator`
- RadioRemote タスクは Ready(Running ではない)
