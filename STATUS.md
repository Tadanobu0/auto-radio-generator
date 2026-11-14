# STATUS

## 受付窓口(b70-gate)向けの overlay を足す(ブリーフ 028)— 2026-11-14 時点
- ブランチ `pm/028` に実装済み。main へは未マージ
- `config/b70gate.overlay.yaml` を新規。中身は `config/b70.overlay.yaml` と同じで `comfyui.base_url` だけ `http://192.168.0.86:8188`(窓口)。誰かが引数か `ARG_CONFIG` で渡すまで読まれない。既存ファイル・コードは変えていない
- 切り替えの日は radio_director の `config_overlay` を `config/b70gate.overlay.yaml` に変える 1 か所だけ
- テスト: `tests/test_b70gate_overlay_028.py`(1 件)。このジョブではテストのコマンドが許可されず**未実行**(merge 前に PM が Mac で流す)

## ComfyUI へ投げる本文の extra_data に sender を入れる(ブリーフ 026)— 2026-11-14 時点
- ブランチ `pm/026` に実装済み。main へは未マージ
- `ComfyUIClient.generate_image` の `POST /prompt` の本文に `extra_data.sender = "radio"`(固定)。投げ先・待ち方・時間切れ・出力は変えていない
- テスト: `tests/test_comfyui_b70_022.py::test_sender_is_in_extra_data` を追加。このジョブではテストのコマンドが許可されず**未実行**(merge 前に PM が Mac で流す)

## B70 の ComfyUI へ Flux を投げている送り主の調査(ブリーフ 025)— 2026-11-13 時点
- 調査のみ(コード・設定の変更なし)。詳細は `.pm/reports/025.md`
- 送り主は ARG の `ComfyUIClient.generate_image`(`config/b70.overlay.yaml` を重ねたとき。`ARG/flux1-schnell` は B70 用ワークフローの写しの filename_prefix)。ARG に起動のしかけは無く、呼ぶのは radio_director の画像段(images_only・candidates=9)
- 3 回の枚数は 9 の倍数(180 / 135 / 153)。11/10 03時台 = 080、11/11 12時台 = 081 の画像段と時刻が合う。11/8 22時台は 022・023 の取り込み直後の B70 経路の通しとみられる(未確認)
- 送り先は設定(overlay の base_url)で替えられる。軽いプロンプトでの確認・切れたときのやり直しは無い(失敗した候補は飛ばし、全部だめなら静止画)

## サムネイル背景の自動プロンプトを「具体物＋題の余白」の形にする(ブリーフ 024)— 2026-11-11 時点
- ブランチ `pm/024` に実装済み。main へは未マージ
- `ThumbnailBackgroundGenerator.build_prompt` → 新しい `ImagePromptGenerator.generate_thumbnail_scene_prompt`。構図(正面の写実・具体物は右 3 分の 1・左 3 分の 2 は無地の面・人と文字なし)はコードで固定し、LLM は PLACE / OBJECTS / SURFACE の 3 行だけ書く。題・要約は日本語のまま渡す
- 抽象画・人・文字の語を含む返答は 1 回やり直し、だめなら具体物の汎用の場面。旧 `generate_thumbnail_prompt` と場面画像のプロンプトは変えていない
- テスト: `tests/test_thumbnail_scene_prompt_024.py`(23 件。パラメータ展開後)。このジョブではテストのコマンドが許可されず**未実行**(merge 前に PM が Mac で流す)。画像での確認もまだ

## B70 の overlay の scheduler を本番と同じ normal にする(ブリーフ 023)— 2026-11-08 時点
- ブランチ `pm/023` に実装済み。main へは未マージ
- `config/b70.overlay.yaml` は 768×432 / steps 4 / cfg 1 / euler / normal で本番と同じ。違うのは送り先(と poll 0.5 秒・timeout)だけ
- ワークフローの写しの scheduler は正本どおり simple のまま(ComfyUIClient が毎回 config の値で書き換えるので送られない)
- テスト: `tests/test_comfyui_b70_022.py` の期待を normal に更新。このジョブではテストのコマンドが許可されず**未実行**(merge 前に PM が Mac で流す)

## 画像段を B70(ComfyUI)へ送る — 調査と案A の ARG 側(ブリーフ 022)— 2026-11-08 時点
- ブランチ `pm/022` に実装済み。main へは未マージ。radio_director 側(Mac で画像段を回す・Windows の予備のスイッチ)は次のジョブ。呼び出し方は `.pm/reports/022.md`
- PM 判断: 案A。大きさは今の本番のまま送り先だけ替える(1344×768 は別の作業)。Windows の画像段は予備。B70 のワークフローは ARG に写し、正本は b70-box。forge-gen と ComfyUI を共用(順番待ち可)
- 切り替え: `ARG_CONFIG=<yaml>` か `run_workflow_sync(config_overlay=<yaml>)` で config.yaml に重ねる。B70 の例は `config/b70.overlay.yaml`(width / height は本番に合わせて書き換える前提)、ワークフローは `config/workflow_b70_flux1_schnell_fp8.api.json`
- ComfyUIClient: ノードは決め打ちの番号か class_type と配線で探す、history の subfolder / type、`comfyui.poll_interval_sec`(既定 2 秒)、`status_str: error` で止める
- キャッシュのキーに種類・送り先・大きさ(localhost だけ前の形のファイルも見る)
- テスト: `tests/test_comfyui_b70_022.py`(27 件)。このジョブではテストのコマンドが許可されず**未実行**(merge 前に PM が Mac で流す)

## 画像段で 1 場面あたり N 枚の候補と一覧画像を作る(ブリーフ 021)— 2026-11-04 時点
- ブランチ `pm/021` に実装済み。main へは未マージ(PM が報告を見てから merge)。radio_director 側(run_remote.py)は次のジョブ。渡し方は `.pm/reports/021.md` の「未解決点」
- `run_workflow_sync` の新しい引数(どれも指定が無ければ今までどおり)
  - `candidates=N`(images_only だけ、1〜16): 各場面を seed 違いで N 枚。`scene_images/NN_<id>_cK.png` と一覧画像 `NN_<id>_sheet.jpg`(ceil(√N) 列・長辺 2048px)。`scene_images.json` の各場面に `candidates` / `contact_sheet`。サムネイル背景(dynamic のときだけ)も `thumbnail_bg_candidates/thumbnail_bg_cK.png` と `thumbnail_bg_sheet.jpg`、`scene_images.json` の `thumbnail_bg`
  - `picks={segment_id: K, "thumbnail": K}`(dict か JSON ファイル。`reuse_images_from` が要る): 前の run の候補 K を使う。thumbnail は本番では `thumbnail_bg` と同じ扱い
  - `prompt_overrides={segment_id: 文 | {focus, draw, avoid}, "thumbnail": 文}`: 作る場面のプロンプトを上書きし、`prompt_override` に記録。verified_script は書き換えない
- 生成時間: 候補ごとに `candidates[].generation_time_sec`。集計は `build_image_candidates_result_fields(result)` の `image_generation_times`
- テスト: `tests/test_image_candidates_021.py` を追加。このジョブではテストのコマンドが許可されず**未実行**(merge 前に PM が Mac で流す)

## タイトル・概要欄から半角 < > を落とす(ブリーフ 020)— 2026-11-02 時点
- ブランチ `pm/020` に実装済み。main へは未マージ(PM が報告を見てから merge)
- `sanitize_for_youtube` が半角 `<` `>` を落とす。参考文献の題が `<16 進>` だけ(PDF の内部名)なら「参考文献N(ドメイン)」。動画タイトルとアップロード直前の保険でも落とす(ログに「< > が残っていた」)
- テスト: `tests/test_description_angle_brackets.py` を追加。このジョブではテストのコマンドが許可されず**未実行**(merge 前に PM が Mac で流す)

## Mac 書き出し用の正式な受け口(ブリーフ 019)— 2026-11-01 時点
- ブランチ `pm/019` に実装済み。main へは未マージ(PM が報告を見てから merge)。radio_director 側の置き換えは別ジョブ(対応表は `.pm/reports/019.md`)
- `run_workflow_sync` の引数(引数が無いときは環境変数)。どれも指定が無ければ今までどおり
  - `reuse_strict=True` / `ARG_REUSE_STRICT=1`: 使い回しの画像が足りなければ止める。合成の前に見積もりで確かめ(`before_synthesis`)、漏れは画像を作る前に止める(`render`)。`reuse_images_from` が要る
  - `thumbnail_bg=<画像>` / `ARG_THUMBNAIL_BG`: サムネイル背景を作らず、その画像を `<run>/thumbnail_bg.png` にコピーして使う
  - `youtube_dir=<フォルダ>` / `ARG_YOUTUBE_DIR`: そのフォルダの client_secret.json / token.json を使い、token が無ければブラウザに進まず失敗(非致命。`upload_error`)
  - `reading_fixes=<JSON>` / `ARG_READING_FIXES`: 合成に渡す文だけを置き換える。字幕は本文のまま。radio_director の `config/reading_fixes.json`(`fixes` 配列の `pattern` 正規表現 → `replace` ひらがな)を並び順に re.sub する。語 → かなの字面の表も受ける
- result.json 用: `build_mac_render_result_fields(result)`(reuse_strict / reuse_strict_failed / reuse_strict_stage / missing_scene_images / thumbnail_bg_source / reading_fixes_path / reading_fixes_applied / upload_error)
- テスト: `tests/test_mac_render_inputs_019.py`(47 件)。最初の 35 件は PM が Mac で流して全通過(全体 912 passed / 1 failed は既知の hugging-face / 30 skipped)。正規表現の形の追加 12 件は未実行(merge 前に PM が Mac で流す)

## 画像だけを作る実行でサムネイル背景も作る(ブリーフ 018)— 2026-11-01 時点
- ブランチ `pm/018` に実装済み。main へは未マージ(PM が報告を見てから merge)
- `run_workflow_sync(images_only=True)` は、場面画像の後に `<run の出力ディレクトリ>/thumbnail_bg.png`(= `scene_images/`・`scene_images.json` と同じ階層)を作る。`thumbnail_background_mode: dynamic` のときだけ。パスは `WorkflowResult.thumbnail_bg_path`(作らなかった・失敗は None)
- 失敗したら `warnings` に `thumbnail background generation failed (scene images are kept): ...` を足し、場面画像の結果はそのまま返す
- 本番の実行(images_only でない)は変えていない
- テスト: `tests/test_images_only_thumbnail_bg_018.py`(5 件)を足した。このジョブではテストを実行できなかった(未実行)

## フォントの受け口 ARG_FONT_DIR と BIZ UDPGothic での書き出し(ブリーフ 017)— 2026-11-01 時点
- ブランチ `pm/017` に実装済み。静止画の見比べは PM 判断で合格(2026-11-01)、merge 待ち
- 方針(PM 判断): 書き出しは Mac に完全移行する。Windows の書き出し経路は移行が済むまでの参照用で、温存の作り込みはしない。budoux==0.8.0 の固定は暫定で、Mac 移行後に見直す
- `services/render_fonts.py`: `ARG_FONT_DIR` が無ければ Meiryo(今までと同じ)。あればそのフォルダの `BIZUDPGothic-Regular.ttf`(必須)/ `-Bold.ttf`(任意)を字幕・章タイトル・日付・サムネイルで使う。ASS の字体名は family 名(`ARG_FONT_NAME` で上書き可)、subtitles に `fontsdir` を付ける
- 字の大きさは em の px が正: 字幕 em 70px → ASS の Fontsize = 70 × (winAscent+winDescent)/unitsPerEm(Meiryo 105)。章タイトル 80〜56・日付 28・サムネイルは em の px のまま
- 字体を替えたときの字幕は L2 方式(1 行 1 Dialogue、MarginV で Meiryo と同じベースライン・行送り 105px)。帯の位置は同じ
- `requirements.txt` の budoux を `==0.8.0` に固定。`test_research_brief_theme_overwrite` の workflow.py を `Path(__file__)` からの相対で読む
- 見比べの静止画: `scripts/render_font_samples.py`。PM が見比べて字幕・章タイトル・日付・サムネイルとも合格
- arg-remote `test -q`: 893 passed / 10 skipped(skip は試しエンコード 8 件と、本物の BIZ UDPGothic が要る 2 件)

## 画像だけを先に作る実行・場面の作り直し・画像の使い回し、置き場所の検査の誤判定(ブリーフ 016)— 2026-10-27 時点
- ブランチ `pm/016` に実装済み。main へは未マージ(PM が報告を見てから merge)。Mac 側(render_local.py → radio-render → launch.ps1 → run_remote.py)は別ジョブ
- 置き場所の検査: draw の「目の高さから見た」とプロンプトの `eye-level view` / `at eye level` / `seen from eye level` 等を高さとみなさない。置き場所の高さ(目の高さの棚)は今までどおり見る
- 短いプロンプト: LLM の 1 文の後ろに、draw の高さ・距離と avoid の逆の状態(肯定形)をコードで足す(`services/script_generation/scene_placement_phrases.py`)
- `run_workflow_sync(images_only=True)`: 背景画像までで止める。画像は `<run>/scene_images/NN_<segment_id>.png`、一覧は `<run>/scene_images.json` と `WorkflowResult.scene_images`、result.json 用は `build_scene_images_result_fields(result)`(欄 `scene_images`)
- `regen_scenes=[...]` / `regen_seed_offset=N`(既定 1): 指定の場面だけキャッシュを使わず seed = 前の seed + N。`reuse_images_from=<前の images_only の run>`: segment_id が合う場面はその画像をそのまま使い、合わない場面・使わなかった画像は warnings に出す
- arg-remote `test -q`: 867 passed / 8 skipped(skip は試しエンコード)

## 画像プロンプトの固定文に「機器の表面は無地」を足す(ブリーフ 015)— 2026-10-26 時点
- ブランチ `pm/015` に実装済み。main へは未マージ(PM が報告を見てから merge)。効き目は merge・deploy 後の /render で見る
- 背景の前置き: 「An unpopulated, wordless scene with no people, no hands, no faces, no text, signage or labels, plain matte unbranded device surfaces: 」(134 字)。足した語句は否定形なし(2026-10-26 PM 判断)。汎用の文・draw の短い英文にも同じ前置きが付く。サムネイルは変えていない
- 指示: visuals の場面の最後の手順に「draw にない記号・図形(光る線や弧・波・矢印・Wi-Fi のマーク)を足さない」を 1 文足した(番号の数は変えない)。draw の短い英文の指示にも 1 行足した。visuals の無い場面の指示文は変えていない
- 既存テストの前置きの上限 `<= 100` を、前置きの長さちょうどの `<= 134` に変えた(PM 了承)
- arg-remote `test -q`: 839 passed / 8 skipped(skip は試しエンコード)

## visuals からのプロンプト作りの取りこぼしを直す(ブリーフ 014)— 2026-10-25 時点
- ブランチ `pm/014` に実装済み。main へは未マージ(PM が報告を見てから merge)
- deep_dive_2_scene1 が汎用の文になった原因(コードからの見立て): LLM の出力が 1000 字を超え、固定の汎用の文に替わった。約 18.6 秒かかったのは、出力が上限(512 トークン)近くまで伸びたためとみられる。失敗の記録は Python の logging だけで、processing_log.txt にも warnings にも出なかった。Windows の run のログ・prompt_report.md・generation_metadata.jsonl はこのジョブから読めず、未確認
- 汎用の文・既定の文(静止画)に替えたときは `WorkflowResult.warnings` に章・場面・理由が入る(処理ログにも `[WARN]`)。例: `image prompt for deep_dive_2 scene 1 (deep_dive_2_scene1) fell back: try 1: LLM output too long (1234 chars > 1000); try 2: …; used a short prompt built from focus/draw`
- scene_visual がある場面: 失敗(例外・空・長すぎ・置き場所のチェック)したら、何が悪かったかを添えて 1 回作り直す → だめなら LLM に focus・draw を 1 文の英語に訳させる → それも答えなければ、退けた出力を使う。汎用の文は LLM がまったく答えないときだけ。visuals の無い場面は従来どおり(作り直さず汎用の文)、warnings だけ足した
- 置き場所のチェック(`services/script_generation/scene_placement_check.py`): draw に高さの語(腰の高さ等)や床から離す語がある、または avoid が床置きを避けるのに、プロンプトに `on the floor` / `low shelf` 等がある → 作り直し。高さの語が家具に付かず機器に付いている(`waist-high white router`)→ 作り直し
- 指示: step 1 に「draw の置き場所・高さ・位置の語を家具の言葉として英語に残す(腰の高さの棚 → on a waist-height shelf)。高さの語は大きさではない」を足した(番号の数は変えていない)
- arg-remote `test -q`: 830 passed / 8 skipped(skip は試しエンコード)

## visuals の avoid の型を仕様に合わせ、警告と画像プロンプトの記録を Mac に返す(ブリーフ 013)— 2026-10-25 時点
- ブランチ `pm/013` に実装済み。main へは未マージ(PM が報告を見てから merge)
- 019 の回で絵が 2 枚のままだった原因(コードからの見立て): `SceneVisual.avoid` が strict な「str か null」で、List[str] の avoid が各章の scene 0 で型エラーになり、3 章とも scene_splitter に戻った。場面数(3・4)は上限 4 以内で、`max_scenes_per_chapter` は visuals の章では見ない。Windows のログ・prompt_report.md はこのジョブから読めず、未確認
- avoid は `List[str]`。str は要素 1 つのリスト、null・欄なしは空リスト。要素が str でなければ不正
- 警告: `WorkflowResult.warnings`(読み込み時の不正 + ターンの時刻が無くて捨てた章)。処理ログにも `[WARN]` で出る
- 画像プロンプトの記録: 背景の分も run のディレクトリ直下の `prompt_report.md` / `generation_metadata.jsonl`(サムネと同じファイル)。キャッシュ命中の場面(`cached: true`)と場面の指定(`scene_visual`)も残す。パスは `WorkflowResult.prompt_report_path` / `generation_metadata_path`、`build_prompt_log_result_fields(result)`
- run_remote.py(radio_director 側)に要る変更は `.pm/reports/013.md`
- arg-remote `test -q`: 804 passed / 8 skipped(skip は試しエンコード)

## Windows 側の実行に「アップロードしない」スイッチを足す(ブリーフ 012)— 2026-10-25 時点
- ブランチ `pm/012` に実装済み。main へは未マージ(PM が報告を見てから merge)
- `run_workflow_sync(skip_upload=True)` か環境変数 `ARG_NO_UPLOAD=1` で、YouTube への公開(サムネ設定・再生リスト追加を含む)だけを飛ばす。制作・メタデータ・サムネ画像は本番と同じ。既定は今までどおりアップロードする
- `WorkflowResult.upload_skipped` を追加。`build_upload_result_fields(result)` が result.json に足す欄 `upload_skipped` / `upload_status`(uploaded / skipped / not_uploaded)/ `video_path` を返す
- run_remote.py・launch.ps1 はこのリポジトリに無い(radio_director 側か Windows 上だけ)。Mac 側の渡し方と、それぞれに要る変更は `.pm/reports/012.md` に書いた(radio_director-018 で行う)
- arg-remote `test -q`: 783 passed / 8 skipped(skip は試しエンコード)

## verified_script の visuals 欄で場面を切り、画像プロンプトに使う(ブリーフ 011、spec v1.11.0 §12 段 1)— 2026-10-25 時点
- ブランチ `pm/011` に実装済み。main へは未マージ(PM が報告を見てから merge)
- loader: `segments[].visuals` を章ごとに検査する(`parse_scene_visuals`)。不正なものは警告を出し、その章だけ scene_splitter に戻す(範囲外・昇順でない・先頭が 0 でない・0 場面・5 場面以上・draw が空・必須欄が無い・型が違う)。ファイル全体は拒否しない
- タイムライン: visuals がある章は start_turn で切る。場面の時刻は従来どおり `turn_start_secs`。各場面のコピーに `ScriptSegment.scene_visual` を付けて画像の依頼に渡す
- 画像プロンプト: focus・draw・avoid を LLM の指示に入れる。台数は英語の数詞で書かせる。avoid は逆の状態を肯定形で描かせ、ネガティブプロンプトには入れない。visuals の無い場面の指示文は変更前と同じ
- 1-c(Wi-Fi の回での確認)は未実施。このジョブからは Wi-Fi 回の verified_script.json と specs リポジトリを読めない。arg-remote にもアップロードなしでレンダリングする手段が無い
- 1-c は別ジョブへ移した(2026-10-25 PM 判断)。先にアップロードなしの /render を別ジョブで作り、その後 radio_director のジョブで Wi-Fi の回を流して確かめる。011 は 1-a・1-b までで完了とし、merge する
- arg-remote `test -q`: 768 passed / 8 skipped(skip は試しエンコード)

## パンの滑らかさと動きの量、章タイトルの帯と日付の重なり(ブリーフ 010)— 2026-10-24 時点
- ブランチ `pm/010` に実装済み。main へは未マージ(PM が報告を見てから merge)。効果は merge・deploy 後の次の /render で見る
- カクつきの原因: crop の x は整数ピクセル(yuv420p では 2 の倍数)にしかならず、90 秒の場面だと「約 1.9 秒止まって 2px 跳ぶ」動きになっていた(実測 2700 コマ中 2500 コマ停止)
- パン: 作業幅 768 に縮めた画像に perspective(sense=source、1/256 画素の精度)で動く窓を写してから、1920x1080 に拡大する。平行移動は 96px → 230px(画面幅の 12%)、ズームは 1.12 ⇔ 1.22。速さは一定で、90 秒より短い場面は途中まで。エンコード前は毎コマ 0.085px ずつ(標準偏差 0.007)、エンコード後は最大 0.39px
- 設定(`video_renderer`): `pan_method: perspective`(`crop` で 009 の方式) / `pan_zoom_min: 1.12` / `pan_zoom_max: 1.22` / `pan_full_range_sec: 90` / `pan_work_width: 768` / `video_track_x264_preset: veryfast`(009 までは ultrafast 固定)
- 映像トラックは 1 分あたり 3.3 秒から 7.5 秒に延びる(14 分の回で約 +1 分)。サイズは 3.1〜5.6 MB/分(ultrafast のままだと 32 MB/分)
- 章タイトル: 帯の左右の余白を 100 → 210px にした(最大幅 1480px。日付は Meiryo 28px で 162px)。空白が続く「？」「！」も主題と副題の区切りにした
- 試しのコマは Windows の `E:\windsurf\arg-test\auto_radio_generator\output\trial_010\` にある(私は見ていない)
- arg-remote `test -q`: 732 passed / 8 skipped(skip は試しエンコード)

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
