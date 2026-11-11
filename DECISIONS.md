# DECISIONS

## 2026-11-11 サムネイル背景のプロンプトは構図をコードで固定し、LLM には場所・具体物・無地の面の 3 行だけを書かせる(ブリーフ 024)
- 決定: `ImagePromptGenerator.generate_thumbnail_scene_prompt` を新設し、`ThumbnailBackgroundGenerator.build_prompt` をこちらに切り替える。固定文は場面画像と同じ前置き(人・手・顔・文字なし)+「正面からの写実的な写真・具体物は右 3 分の 1 の右端寄りに小さく・左 3 分の 2(中央を含む)は無地の平らな面で題のための余白」+ブランド色(neon lighting・キーワードは付けない)+フィルムの語。題・題名・要約は日本語のまま LLM に渡す。抽象画・人・文字の語(abstract / crystal / particle / light web / person / hand / logo / label 等。face down は可)を含む返答は 1 回やり直し、だめなら具体物の汎用の場面(居間のサイドテーブルとランプ)に落とす
- 理由: 旧指示文が ABSTRACT・具体物なしを求め、日本語の題は `_extract_english_keywords` で "abstract concept, modern technology, innovative ideas" に置き換わり、080・081 とも LLM は同じ中身のない入力しか受けていなかった。合格した上書きは 2 回とも同じ型だった
- 余白の位置: ARG の題は中央寄せ・幅最大 648px(x 316〜964)。合格した 080・081 の上書きは「右 3 分の 1 に物・左 3 分の 2 が余白」でこの題の下で通ったので、その型に合わせ、物は右端寄り・小さくと書く
- 却下案: 旧メソッド `generate_thumbnail_prompt` を書き換える(既存テストが中身と抽象画のフォールバックを固定しており「テストは追加のみ」に反する。旧メソッドは残し呼ばれなくなるだけ)/ 余白を題の位置どおり中央に取り物を左右両端に置く(合格例と違う型で未検証)/ プロンプト全体を LLM に書かせて検査だけする(構図が LLM 任せに戻る)

## 2026-11-08 B70 の overlay の scheduler を本番と同じ normal にし、ワークフローの写しは simple のまま残す(ブリーフ 023)
- 決定: `config/b70.overlay.yaml` の `comfyui.scheduler` を simple → normal(768×432・steps 4・cfg 1・euler は変えない)。`config/workflow_b70_flux1_schnell_fp8.api.json` の KSampler の scheduler(simple)は変えない
- 理由: radio_director-078 で本番(Windows の ComfyUI)は euler / normal と確認。PM の方針は「送り先だけを替える」。ワークフローを読む経路は `ComfyUIClient.generate_image` だけで、送る前に必ず config の scheduler で書き換える(`ComfyUIConfig.scheduler` は既定 normal で必ず値がある)ので、写しの値は送られない
- 却下案: 写しの JSON も normal にする(正本 b70-box の `flux1-schnell-fp8.api.json` と写しがずれる。送る中身は変わらない)

## 2026-11-08 画像段は Mac の arg-render から B70 の ComfyUI へ送る(案A。ブリーフ 022、PM 判断)
- 決定: 案A。Mac で `run_workflow_sync(images_only=True)` を回し、ARG の ComfyUIClient が B70(http://192.168.0.70:8188)へ送る。大きさは今の本番と同じにして送り先だけ替える(1344×768 への変更は別の作業。ARG は大きさを設定で選べるようにするだけ)。Windows の画像段は予備として残し、切り替えのスイッチは radio_director 側に置く。B70 のワークフロー JSON は ARG に写して持ち、正本は b70-box と README に書く。B70 の ComfyUI は forge-gen と共用し、キューの順番待ちは許す(VRAM は常駐のまま)
- 理由: ARG にはもう ComfyUI のクライアントがあり、画像段の機能はすべて `generate_image(..., seed)` の上で動く。Windows を起こす経路(使用中の終了コード 20)が画像段から消える
- 却下案: 案B Windows の ARG のまま送り先だけ B70(起こす・使用中で止まるが残る)/ 案C forge-gen を共通部品として呼ぶ(2 部品の版ずれ、seed 制御が未確認)/ ワークフローを b70-box から参照する(Mac の配置に依存する)

## 2026-11-08 B70 用の設定は ARG_CONFIG の yaml を config.yaml に重ねて切り替える(ブリーフ 022)
- 決定: 環境変数 `ARG_CONFIG`(または `run_workflow_sync(config_overlay=...)`)に yaml のパスを渡すと、config.yaml を読んだ後にその yaml を深く重ねる(dict はキーごと、それ以外は置き換え)。相対パスはプロジェクト直下から。ファイルが無い・壊れているときは例外で止める(黙って 127.0.0.1 に送らない)。例は `config/b70.overlay.yaml`
- キャッシュ: クライアントが `cache_identity()`(種類・送り先・大きさ)を出すときは、キーを `sha256(identity + "\n" + prompt)` にする。送り先が 127.0.0.1 / localhost のときだけ、見つからなければ前の形(`sha256(prompt)`)のファイルも見る(Windows の今のキャッシュがそのまま当たる)
- ComfyUI のノード: KSampler は決め打ちの番号が KSampler ならそれ、違えば class_type で探す。プロンプト・潜在画像は KSampler の配線からたどり、だめなら決め打ちの番号。SaveImage も同じ。取り出しは history の subfolder / type を使う。ポーリング間隔は `comfyui.poll_interval_sec`(既定 2 秒)
- 理由: config.yaml の全体を写すと本番の設定とずれる。重ねる形なら B70 の差分だけを持てる。キャッシュの前の形を local だけで見るのは、Windows の今の動きを変えないため
- 却下案: ARG_CONFIG で config.yaml を丸ごと置き換える(写しが古くなる)/ キャッシュの前の形を捨てる(Windows のキャッシュが一度すべて外れる)/ ノードを番号の決め打ちのまま(B70 の形が違うと黙って静止画に落ちる)

## 2026-11-04 画像の候補は引数 candidates、選択と上書きは引数 picks / prompt_overrides で受ける(ブリーフ 021)
- 決定: `run_workflow_sync` に `candidates`(images_only だけ。1〜16、既定 1)/ `picks` / `prompt_overrides` を足す。picks と prompt_overrides は dict か JSON ファイルのパス。picks は `{segment_id: 番号, "thumbnail": 番号}`(1 始まり)で `reuse_images_from` が要り、無い番号は合成の前に止める。images_only でない実行に candidates ≥ 2 を渡したら止める
- seed: 候補 K の seed = 始まり + (K−1)。始まりは segment_id から決めた固定値(016 の base_seed_for)。regen_scenes の場面は「前の run の候補の一番大きい seed + regen_seed_offset」から(016 の「前の seed + offset」を候補に広げた形。前に見た絵が戻らない)。サムネイル背景はキー "thumbnail" で同じ決め方。プロンプトと seed が同じ候補はキャッシュ(`<hash>_seed<seed>.png`)を使う
- 使い回し: 候補ごと次の run に写す(一覧画像も作り直す)。サムネイル背景も、前の run に候補があり regen_scenes に "thumbnail" が無ければ作り直さない(候補 2 以上か "thumbnail" の上書きのときだけ。既定の 018 の動きは変えない)
- プロンプトの上書き: 文は「人・文字なし」の前置きを付けてそのままプロンプトにする。`{focus, draw, avoid}` は台本の描く指示をその場面だけ差し替えて、いつもの LLM のプロンプト作りに通す。効くのは作る場面(regen_scenes・使い回しの無い場面)だけで、使い回した場面の上書きは warnings に出す
- 一覧画像: ceil(√N) 列、長辺 2048px、JPEG(品質 90)。番号は各タイルの左上に黒地白字(タイルの高さの 28%)。失敗した候補は灰色のタイルにして番号を残す
- 理由: run_remote.py は run_workflow_sync を直接呼ぶので、引数なら Windows の run にファイルを送らずに済み、前の run も書き換えない。上書きを作る場面だけに効かせると、「作り直すかどうか」の切り替えが regen_scenes 1 つで済む
- 却下案: 前の run に picks.json を置く(送り込みが要り、前回の残りに黙って従う)/ 上書きした場面を自動で作り直す(本番の実行に上書きを渡すと黙って作り直してしまう)/ 一覧画像を PNG にする(3×3 で数 MB になる)/ 候補の seed を乱数にする(同じ候補を作り直せず、キャッシュも効かない)

## 2026-11-02 タイトル・概要欄の半角 < > は全角に置き換えず「落とす」(ブリーフ 020)
- 決定: `sanitize_for_youtube` で半角 `<` `>` を削除する(`strip_angle_brackets`)。参考文献の題が `<16 進>` だけ(PDF の内部名)なら題を出さず「参考文献N(ドメイン)」にする(`is_hex_only_title`。`format_reference_label` の中なので概要欄と metadata.txt の両方に効く)。動画タイトル(`_build_publish_fields` の `formatted_title`)と、アップロード直前の保険(`_upload_to_youtube`)でも同じく落とす。全角 ＜ ＞ は触らない
- 理由: 既存の処理(制御文字・URL)は「落とす」流儀で、radio_director 側(phase_d/metadata_generator.resolve_references)も落としている。両経路で同じ見た目にそろう。題の中の < > は「<速報>」のような囲みが多く、外しても読める
- 却下案: 全角 ＜ ＞ に置き換える(radio_director 経路と表記がずれる。PDF 内部名の残りが目立つ)/ 参考文献の題だけ直す(Discord /run などで概要文・タイトルに入ったときに同じ失敗が起きる)

## 2026-11-01 Mac 書き出しの 4 つの差し替えを run_workflow_sync の引数(と環境変数)で受ける(ブリーフ 019)
- 決定: `run_workflow_sync` に `reuse_strict: bool = False` / `thumbnail_bg: str|None` / `youtube_dir: str|None` / `reading_fixes: str|None` を足す。引数が無いときだけ環境変数 `ARG_REUSE_STRICT`(1/true/yes/on)/ `ARG_THUMBNAIL_BG` / `ARG_YOUTUBE_DIR` / `ARG_READING_FIXES` を見る。指定したファイル・フォルダが無い・読めないときは、合成の前に `success=False` で止める。result.json 用の欄は `build_mac_render_result_fields(result)`
- reuse_strict: `reuse_images_from` が要る。合成の前に `plan_scene_ids`(images_only と同じ区切り)で場面を見積もり、使い回しの画像が無い場面(regen_scenes は除く)があれば止める(stage `before_synthesis`)。見積もりで漏れた場面は、本番の画像の段で `ImageProvider._reused_image` が `ReuseStrictError` を投げて止める(stage `render`。合成の後・画像を作る前)。余った画像(この run で使わなかった画像)は今までどおり warnings だけ
- youtube_dir: `YouTubeClient(config, credentials_dir=...)` がそのフォルダの client_secret.json / token.json を使い、token が無い・更新できないときはブラウザの同意に進まず `YouTubeAuthError`。アップロードの失敗が非致命なのは今までと同じで、理由は `WorkflowResult.upload_error`
- reading_fixes: 合成に渡す文(`_synthesize_phrase` の中、引用タグを外した後)だけを置き換える。字幕は本文のまま。radio_director の形(`{"fixes": [{"pattern", "replace", "reading", ...}]}`)は `pattern` を正規表現として並び順に re.sub し、置き換え先は `replace`(`reading` は説明用で使わない)。読み込み時に radio_director と同じ検査(正しい正規表現・`search("")` が None・replace に後方参照なし)と、replace の不正なエスケープの検査をする。前からの語 → かなの形も残し、正規表現の規則の後に長い語から 1 回だけ置き換える(2026-11-01 PM 追加指示)
- 理由: run_remote.py は run_workflow_sync を直接呼ぶので、引数なら名前が変わると TypeError で気づける。環境変数は 012 と同じ流儀で、起動側だけで切り替えたいときのため
- 却下案: config.yaml に持つ(Windows と Mac で値を分けにくい)/ 環境変数だけ(綴りの誤りで黙って既定に戻る)/ reuse_strict を合成の後の確認だけにする(ブリーフの「合成の前に止めたい」に合わない)/ 置き換え表を字面の単純置換だけにする(「数値」「値段」にも当たる)

## 2026-11-01 画像だけを作る実行のサムネイル背景は、本番と同じ thumbnail_background_mode に従う(ブリーフ 018)
- 決定: `images_only=True` のとき、場面画像の後に `generate_images_only_thumbnail_bg` で `<run の出力ディレクトリ>/thumbnail_bg.png` を作る。作るのは `video_renderer.thumbnail_background_mode == "dynamic"` のときだけ(config.yaml の本番値は dynamic)。失敗は例外にせず `WorkflowResult.warnings` に 1 行足し、場面画像の結果はそのまま返す。パスは `WorkflowResult.thumbnail_bg_path`
- 理由: 本番の実行(`_generate_thumbnail_assets`)と同じ設定で同じ動きにそろう。static にしている環境では FLUX の時間を使わず、Mac 側は今までどおり静的背景になる。本番経路の関数は既存テストがスタブにしているので触らず、別の関数を足した
- 却下案: 設定に関係なく images_only では必ず作る(static の意図を無視する)/ `_generate_thumbnail_assets` を流用する(thumbnail.png とメタデータが要り、静的背景へのフォールバックも images_only では不要)

## 2026-11-01 書き出しは Mac に完全移行し、Windows の書き出し経路は参照用としてだけ残す(ブリーフ 017、PM 判断)
- 決定: 書き出しは Mac に完全移行する(PM が成功を確信)。Windows の書き出し経路は、移行が済むまでの参照用としてだけ残し、温存のための追加の作り込みはしない。017 の静止画の見比べ(字幕・章タイトル・日付・サムネイル)は PM 判断で合格とし、merge する
- 理由: BIZ UDPGothic での静止画が Meiryo の本番と見比べて問題なかった。両機で書き出しを保つと、字体・ffmpeg・依存の差を二重に面倒みることになる
- 却下案: 両機で書き出しを保ち続ける / Windows の経路に Mac と揃えるための作り込みを足す

## 2026-11-01 budoux==0.8.0 の固定は暫定の措置(ブリーフ 017、PM 判断)
- 決定: `requirements.txt` の `budoux==0.8.0` は、移行中に Windows と Mac で字幕の改行を揃えるための暫定の固定。Mac 移行後に見直す
- 理由: 書き出しが Mac だけになれば、両機で揃える必要は無くなる
- 却下案: 恒久の固定にする(更新を止めたままになる)

## 2026-11-01 書き出しの字体は ARG_FONT_DIR で受け、ASS の Fontsize は em の px から出す(ブリーフ 017)
- 決定: `services/render_fonts.py` に受け口を 1 つ置き、字幕・章タイトル・日付・サムネイルの 4 か所がそこを見る。`ARG_FONT_DIR` が無ければ Meiryo(今までと同じ文字列・同じ挙動)。あればそのフォルダの `BIZUDPGothic-Regular.ttf`(必須。無ければ `RenderFontError` で止める)と `-Bold.ttf`(任意)。ASS の Fontname はファイルの name テーブルの family 名(`ARG_FONT_NAME` で上書き可)。libass には subtitles の `fontsdir` でフォルダを渡す。ASS の Fontsize は em 70px × (usWinAscent + usWinDescent) / unitsPerEm(Meiryo は 1.5 で 105)。drawtext・Pillow はもともと em の px なので数字はそのまま。字幕は字体を替えたときだけ 1 行 1 Dialogue(L2)にし、MarginV で Meiryo を \N でつないだときと同じベースラインに置く(行送り 105px、descent の差を埋める)。帯の位置は変えない。4 か所とも Regular(Meiryo の今の太さの扱いと同じ)
- 理由: 既存の本番を変えない(Append-Only)。libass は Fontsize を win の高さで解釈するので、em を正にすると字体が替わっても字の大きさが揃う。指定したのに黙って Meiryo に落ちると Mac では豆腐になる
- 却下案: 字体名と大きさを config.yaml に持つ(Windows と Mac で別の値を持つ必要があり、環境変数の方が機ごとに切りやすい)/ Meiryo でも L2 にする(既存の ASS が変わる)/ Pillow の getmetrics(hhea)で換算(libass は OS/2 の win を使う)/ 字体の行送りのまま \N でつなぐ(BIZ UDPGothic は 1.0em に詰まる。PM 決定で L2)

## 2026-10-27 画像だけ先に作る実行・場面の作り直し・画像の使い回しの受け渡しの形(ブリーフ 016)
- 決定: `run_workflow_sync` に `images_only` / `regen_scenes` / `regen_seed_offset`(既定 1)/ `reuse_images_from` を足す。images_only は台本の読み込み → 場面の区切り(本番と同じ `TimelineCalculator.plan_scene_segments`)→ 背景画像で止め、画像を `<run>/scene_images/NN_<segment_id>.png` にコピーし、一覧を `<run>/scene_images.json` と `WorkflowResult.scene_images` に入れる。`build_scene_images_result_fields(result)` が result.json の欄 `scene_images` を返す。reuse_images_from はその json を読み、segment_id(重複は出た順)で対応づける。作り直しはキャッシュを使わず、seed = 前の run の seed + offset(不明なら segment_id のハッシュ + offset)。キャッシュの画像は上書きせず `<hash>_seed<N>.png` に書く
- 理由: segment_id は場面の区切りが同じなら本番と一致する。プロンプトは毎回 LLM が作るのでキャッシュ(プロンプトのハッシュ)では当たらない。seed を決めておけば、作り直した結果が result.json から追える
- 却下案: 章・場面の番号で対応づける(章の増減でずれる)/ 作り直しも乱数の seed(再現できない)/ 本番の実行でも毎回コピーする(指定が無いときの動きを変えない)
- 補足: visuals の無い章は、音声を作らないので発話の長さを文字数(8 字/秒 × 話速、間は pause_between_phrases_ms)から見積もって切る。本番と場面の数が変わったら、合わない場面は作り直して warnings に出す。images_only でも前提条件の確認(VOICEVOX・FFmpeg)はそのまま通す

## 2026-10-27 置き場所の検査は視点の言い方を除き、短いプロンプトには avoid の逆と置き場所の語をコードで足す(ブリーフ 016)
- 決定: draw の「目の高さから / 目の高さで見た / 目の高さの視点」と、プロンプトの `eye-level view` / `at eye level` / `seen from eye level` / `eye-height camera` 等(腰・胸・肩も同じ)を、高さの検査の前に取り除く。短いプロンプト(`_short_scene_prompt`)には、LLM の 1 文の後ろに、draw の高さ(`at waist height`)・距離(`with about 30 centimeters of open space around it`)と、avoid の逆の状態(表で決めた肯定形。例 窓際 → `away from the window, toward the middle of the room`)を、文にまだ無ければ足す
- 理由: 加湿器の回で外れた 4 場面はすべて短いプロンプトで、LLM の 1 文が avoid と置き場所を落としていた。指示を強めるだけでは同じことが起きうる。表に無い avoid は LLM への指示(肯定形で書く)にだけ頼る
- 却下案: 短いプロンプトの LLM に任せたまま指示だけ強める / avoid を LLM でもう 1 回英訳する(呼び出しが増え、また落としうる)/ 視点の語を draw から台本側だけで消す(古い台本で同じことが起きる)

## 2026-10-26 背景の前置きに「plain matte unbranded device surfaces」を足し、前置きの上限を 100 → 134 字に広げる(ブリーフ 015、PM 判断)
- 決定: `SEGMENT_PROMPT_PREFIX` の末尾に否定形の無い「plain matte unbranded device surfaces」だけを足す(→ 134 字)。007 の既存テスト `test_prefix_is_short_and_has_no_screen` の `len(prefix) <= 100` を、この長さちょうどの `<= 134` に変えた(他の検査はそのまま。PM 了承)。draw にない記号を足させない指示は、visuals の場面の最後の手順(番号の数は変えない)と、draw の短い英文の指示に 1 文ずつ足した。visuals の無い場面・サムネイルは変えない
- 理由: FLUX schnell は否定の名詞を名詞として読み、「no logos」がロゴを呼ぶおそれがある(2026-10-26 PM 判断)。前置きと本文をつなぐ形(`PREFIX + body`)は多くの既存テストが見ていて、別の定数に分けても落ちるので、上限を広げるのが変更が一番小さい
- 却下案: ブリーフの例文「… with no logos, model names or printed characters」(179 字。否定の名詞を描くおそれ。2026-10-25 に一度入れて PM 判断で撤回)/ 100 字に収まるよう「logos」だけ足す / 本文の後ろに付ける(先頭に置く方針と既存テストに反する)/ 新しい番号の手順を足す(既存テストが番号の数を見ている)

## 2026-10-25 場面の指定がある場面のプロンプトは、作り直し → draw の短い英文の順に落とし、汎用の文は使わない(ブリーフ 014)
- 決定: scene_visual がある場面でプロンプト生成が失敗したら(例外・空・1000 字超・置き場所のチェック)、理由を添えて 1 回作り直す。だめなら LLM に focus・draw(と avoid の逆の状態)を 1 文の英語に訳させ、前置き・配色・フィルムの語を付ける。それも LLM が答えなければ、退けた出力を使う(置き場所が崩れたものはそのまま、長すぎたものはカンマで切る)。汎用の文は LLM がまったく答えないときだけ。替えたときは、章・場面・理由を `ImagePromptGenerator.warnings` → `ImageProvider.warnings` → `RenderResult.warnings` → `WorkflowResult.warnings` と運ぶ。FLUX 失敗で静止画にしたときも同じ。visuals の無い場面は従来どおり作り直さず、warnings だけ足す
- 置き場所のチェックは「draw(か avoid)が床から離すのに `on the floor` / `low shelf` 等がある」「draw の高さの語が家具に付かず機器に付いている(`waist-high white router`)」の 2 つだけを弾く。語が無いことは弾かない
- 理由: draw は日本語で、LLM 抜きでは英語にできない。訳だけの呼び出しは短く暴走しにくい。語の有無まで見ると、既存テストの出力(「on a wooden shelf」)が通らず、場面ごとの呼び出しも増える
- 却下案: draw を決まった英文に組む(日本語を訳せない)/ 長い出力を切って先に使う(ブリーフの「focus・draw をもとにした英文」から外れ、主題がぼやける)/ step を 1 つ足して指示を強める(既存テストが番号の数を見ている。step 1 の文を足した)

## 2026-10-25 visuals の avoid は List[str] で受け、警告と画像プロンプトの記録を WorkflowResult に載せる(ブリーフ 013)
- 決定: `SceneVisual.avoid` を `List[str]`(既定は空リスト)にする。str 1 つは要素 1 つのリスト、null は空リストとして受け、空白だけの要素は落とす。要素が str でないものは不正(その章は scene_splitter)。プロンプトでは要素を `; ` でつないで「must NOT show」の 1 行にする。不正な visuals の警告は `ScriptSegment.visuals_warning` → `ExternalScriptPhaseResult.warnings`、タイムラインで捨てたものは `TimelineCalculator.warnings` → `RenderResult.warnings` → `ProductionPhaseResult.warnings` と運び、`WorkflowResult.warnings` にまとめる。画像プロンプトの記録(`prompt_report.md` / `generation_metadata.jsonl`)は、背景の分も run のディレクトリ直下(サムネと同じファイル)に書き、キャッシュ命中の場面と場面の指定(scene_visual)も残す。パスは `WorkflowResult.prompt_report_path` / `generation_metadata_path` と `build_prompt_log_result_fields(result)` で返す
- 理由: interface_spec §12 では avoid は List[str](011 のブリーフの要約の誤り)。Mac に返るのは result.json だけで、そこを書くのは radio_director 側の run_remote.py。背景の記録は `videos/` の下、サムネの記録は run の直下と 2 か所に分かれていて、キャッシュ命中の場面は記録されなかった
- 却下案: str だけ受けるまま(仕様と違う)/ logging のハンドラで警告を拾う(何でも拾い、どこで捨てたかが曖昧)/ 記録を `videos/` の下に残す(サムネと分かれ、Mac が 2 か所を取りに行く)


## 2026-10-25 アップロードしない実行は環境変数 ARG_NO_UPLOAD と skip_upload 引数で切る(ブリーフ 012)
- 決定: `run_workflow_sync(skip_upload=True)` か、環境変数 `ARG_NO_UPLOAD` が `1`/`true`/`yes`/`on` のとき、YouTube への公開(`_upload_to_youtube` 全体。サムネ設定・再生リスト追加を含む)だけを飛ばす。`upload_override` より優先する。既定(どちらも無し)は今までどおり。結果は `WorkflowResult.upload_skipped` に入れ、`build_upload_result_fields(result)` が result.json に足す欄(`upload_skipped` / `upload_status` / `video_path`)を返す
- 理由: run_remote.py と launch.ps1 はこのリポジトリに無い(履歴にも無く、Windows の本番ツリーにも無い)。環境変数なら launch.ps1 がそのプロセスにだけ設定すれば、run_remote.py の改修前でも公開が止まる。止める向きなので、どこから来ても優先して安全
- 却下案: config.yaml の `publishing.enable_upload` を切る(ファイルの書き換えが要り、戻し忘れで本番の公開が止まる)/ user_settings.json に項目を足す(upload の項目が無く、UI の設定保存と混ざる)/ 引数だけ(run_remote.py の改修が済むまで使えない)

## 2026-10-25 011 の 1-c(Wi-Fi の回での確認)は別ジョブへ移す(PM 判断)
- 決定: 011 は 1-a・1-b までで完了とし、merge する。1-c は、先にアップロードなしの /render を別のジョブで作り、その後 radio_director のジョブで Wi-Fi の回を流して確かめる
- 理由: このジョブからは Wi-Fi の回の verified_script.json(worktree の外)を読めず、arg-remote にもアップロードなしでレンダリングする手段が無い
- 却下案: 写しを worktree に置いて続きのジョブで確かめる(レンダリングの手段が無いことは変わらない)

## 2026-10-25 visuals 欄は loader で章ごとに検査し、avoid は逆の状態を描く指示にする(ブリーフ 011、spec v1.11.0 §12)
- 決定: `VerifiedScript` の `Segment.visuals` は形を問わず(`Optional[Any]`)受け、`build_script_segments` の `parse_scene_visuals` で章ごとに検査する。正しいものだけ `ScriptSegment.visuals`(SceneVisual は strict。`"3"`・`true`・`3.0` を start_turn として受けない)に入れる。不正なら警告して None にし、その章は scene_splitter に戻す。タイムラインでは start_turn で切り、各場面のコピーに `scene_visual` を付けて画像の依頼に渡す。画像プロンプトの LLM 指示には focus・draw・avoid を「Scene Direction」として入れる。avoid は「逆の状態を肯定形で描き、避ける状態は否定形でも書かない」と指示する
- 理由: 仕様では、不正な visuals があってもファイル全体を拒否しない。モデルで型付けすると ValidationError で回全体が止まる。ターン数がわかる loader なら範囲の検査もできる。FLUX schnell(cfg 1.0)はネガティブプロンプトが効かず、否定形で書いた物は呼び込む(006・007 の知見)
- 却下案: モデルで SceneVisual を型付けする(1 章の不正で回全体が止まる)/ タイムラインで検査する(検査が 2 か所に分かれる)/ avoid をネガティブプロンプトに入れる(仕様で禁止。効かない)
- 補足: visuals がある章は `max_scenes_per_chapter` を見ない(上限は仕様の 4 場面)。ターンの時刻が無い章(推定タイミング)は警告を出して scene_splitter に戻す。visuals の無い場面の指示文は変更前と同じ文面

## 2026-10-24 パンは perspective で小数ピクセルずつ動かし、ズームを足す(ブリーフ 010)
- 決定: 画像を作業幅 768 にして、perspective(sense=source、eval=frame、コマ番号 `in` で進める)で動く長方形の窓を写し、1920x1080 に拡大する。画像の中央を画面幅の 12% 平行移動させ、ズームを 1.12 ⇔ 1.22 倍で動かす。速さは 90 秒で全部動く速さに揃える(短い場面は途中まで、長い場面はゆっくり)。009 の crop は `pan_method: crop` で残す
- 理由: crop・zoompan・overlay は位置が整数ピクセルにしかならず、ゆっくりした動きが「止まって跳ぶ」になる。perspective は 1/256 画素で補間でき、窓を長方形にすればゆがまない。作業幅 1920 だと 1 分 19.8 秒かかるが、768(FLUX の出力の幅)なら 6.4 秒
- 却下案: zoompan(整数ピクセル)/ 大きく拡大してから crop(拡大した画像を毎コマ処理するので重い)/ 作業幅 1920 や 960(時間が 2.6〜6.4 倍)/ 平行移動だけで量を増やす(必要な拡大率が上がり、ズームなしでは動いて見えにくい)

## 2026-10-24 映像トラックの x264 の preset を ultrafast → veryfast にする(ブリーフ 010)
- 決定: `video_track_x264_preset: veryfast` を既定にする(libx264 のまま。009 の PM 判断のうち preset の部分を変更)
- 理由: ultrafast は動きの探索が整数ピクセルだけで、毎コマ小数ピクセル動く絵だと 1 分 32 MB(14 分で約 450 MB、mux はコピーなのでそのままアップロードの大きさ)。veryfast は 5.6 MB で、時間は 1 分あたり +1 秒
- 却下案: ultrafast のまま(サイズが約 10 倍)/ superfast(29 MB/分)/ NVENC(13 MB/分。時間は同程度。`video_track_use_gpu` で切り替えはできる)

## 2026-10-24 章タイトルの帯は最大幅を狭めて日付を避ける(ブリーフ 010)
- 決定: 帯は中央揃えのまま、左右の余白を「日付の幅 170 + 右端の余白 20 + 間隔 20」= 210px にする(最大幅 1700 → 1480px)。空白が続く「？」「！」も主題と副題の区切りにする
- 理由: 帯の位置と日付の位置を変えずに済み、どの題でも重ならない。「電波はどこで弱まる？ 距離・壁・水と金属」が「？」で 2 行に分かれる
- 却下案: 日付を下げる・左上に移す(画面の配置が変わる)/ 帯を下げる(字幕以外の画面が狭くなる)/ 長い題だけ右に寄せない配置にする(題ごとに位置が変わる)

## 2026-10-23 背景画像は「小(プロンプト改善)→ 中(枚数増 + パン)」の順に進める(ブリーフ 006、PM 判断)
- 決定: 小(画像プロンプトの指示文の改善)→ 中(章を約 90 秒ごとに割って枚数を増やし、ゆっくりしたパンを入れる)の順に、それぞれ別のブリーフで進める。大(radio_director の台本に `segments[].visuals` を足す)は中の後に判断する。パンによるファイルサイズの増加は、中のブリーフで 1 分ぶんの試しエンコードを測ってから決める(2 倍を超えるなら映像トラックを NVENC にする案を併記)
- 理由: 絵と話のずれの原因は指示文(枕元の例・画面の禁止・章の先頭 800 字だけ)にあり、小が安くて効く。中は Windows 側だけで閉じる。大は radio_director と仕様書の変更を伴い、中で場面の単位ができれば後から受け口を足せる
- 却下案: 先に大から入る(リポジトリをまたぐ変更で重い)/ パンのサイズの方針を測る前に決める(エンコード時間とサイズが見積もりしかない)

## 2026-10-23 字幕の帯は ASS の描画イベントで敷く(ブリーフ 004)
- 決定: キューごとに Band スタイルのベクター描画(Layer 0、黒・不透明度約 62%、上端 \blur12)を出し、字幕本文は Layer 1 に置く。帯の高さは行数に合わせる
- 理由: 話者ごとの縁の色(OutlineColour)を残したまま帯を敷ける。ffmpeg 側のフィルタを変えずに済む
- 却下案: BorderStyle 3(箱の色が OutlineColour と共用になり話者の縁色が消える)/ BorderStyle 4(libass の拡張で、本番 ffmpeg の libass が対応しているか確かめられない)/ drawbox で常時帯(字幕が無い間も画面が暗くなる)

## 2026-10-23 字幕の分割は切れ目の種類に重みを付けた DP で選ぶ(ブリーフ 004)
- 決定: 切れ目候補(文末・読点・文節)をまとめる分け方のうち、2 行以内を守りつつ「文節の切れ目 → 読点の切れ目 → 10 字未満のキュー → キュー数 → 長さのばらつき」の順に少ないものを選ぶ
- 理由: 旧実装(前から貪欲にまとめる)では「、」で切れた残りが短く単独で出るのを避けられなかった。規則の優先順位をそのまま式にできる
- 却下案: 貪欲のまま後処理で短いキューを繋ぐ(2 行の上限で繋げない場合に、読点の位置を選び直せない)

## 2026-10-23 改行位置の修正は既存の BudouX の上に規則を足す(ブリーフ 004)
- 決定: 新しい依存は入れない。BudouX の文節のうち、助詞・いう/もの/して/こと で始まるものを前の文節に繋げる。「できる」「もっと」など助詞と同じ字で始まる自立語は例外表で除く
- 理由: 問題の箇所(「CO2その/ものは」「行頭のいう」)は文節の頭の語で判定でき、形態素解析器を足すほどではない
- 却下案: 形態素解析(fugashi / SudachiPy 等)を足す(Windows 本番への pip install が要り、このジョブでは入れられない)

## 2026-10-23 章タイトルは drawtext のまま 1 行 1 フィルタで折り返す(ブリーフ 004)
- 決定: 2 つのレンダラーの章タイトル処理を `services/video_rendering/topic_overlay.py` に寄せ、Meiryo の実測幅で 2 行まで折り返し、収まらなければ字を小さくする。drawtext の字体の第一候補を MS ゴシックから Meiryo に替えた(右上の日付も Meiryo になる)
- 理由: 台本(verified_script.json)の形式を変えずに済む。行ごとに drawtext を置けば行ごとに中央揃えにできる
- 却下案: 章タイトルも ASS に入れる(字幕 ASS は音声合成側、章タイトルはレンダラー側で作っており、受け渡しを変える必要がある)/ 1 つの drawtext に改行を入れる(古い ffmpeg では行ごとの中央揃えができない)

## 2026-10-23 章タイトルは「：」「: 」で主題と副題に分ける(ブリーフ 005)
- 決定: 最大の字(80px)で 1 行に入らない題に「：」か「: 」(空白つきの半角)があれば、主題・副題がそれぞれ 1 行に入る最大の字で、そこで 2 行に分ける。「：」は 1 行目の末尾に残す。入らなければ従来の実測幅の折り返しにし、行頭に来た「：」は前の行の末尾に移す
- 理由: 文字を落とさない既存方針のまま、「：」が 2 行目の頭に来なくなる。空白の無い半角「:」(「10:30」)は区切りにしない
- 却下案: 「：」を落とす(元の題と表示が食い違う)/ 1 行に入る短い題も「：」で分ける(短い題が不要に 2 行になる)

## 2026-10-23 章タイトルの drawtext は expansion=none にする(ブリーフ 005)
- 決定: 章タイトルの drawtext に `expansion=none` を付け、text を展開せずそのまま描く
- 理由: 既定(expansion=normal)では「%」を展開記法として読み、「%{」以外が続くと題を描かない。Windows の ffmpeg で、外すと描かれないことをテストで確かめた
- 却下案: 「%」を `\\%` にエスケープする(フィルタグラフと option の 2 段のエスケープを合わせる必要があり、壊れやすい)

## 2026-10-23 背景の切り替えを章タイトルの切り替わりに合わせる(ブリーフ 005)
- 決定: `TimelineCalculator` の最後に `align_cuts_to_segment_starts` を通し、背景の境界を次のセグメントの最初の発話の時刻(= 章タイトル・YouTube チャプターの時刻)に置く。最初の背景は 0 秒から、最後の背景は音声の終わりまで
- 理由: 各セグメントの長さ(最初の発話〜ジングルの終わり)を足し並べていたため、冒頭 2 秒の無音と各セグメント最後の発話の後の 250ms が抜け、k 番目の境界が 2 + 0.25k 秒早かった
- 却下案: 章タイトルの時刻を背景の側に合わせる(背景側の値がずれているので、正しくない値に揃えることになる)

## 2026-10-23 画像プロンプトの本文抜粋は各論点の最初の引用ターンを中心に取る(ブリーフ 007)
- 決定: 外部台本の `show_spec.topics[i].key_claims[].source_idx` を loader で `ScriptSegment.key_claim_source_idxs` に運ぶ。抜粋は、各番号を最初に引く `[src=N]` のターンを中心に、前後 1 ターンを入るときだけ足す。番号が無い・見つからない章は 4 等分の位置。合計 800 字(従来と同じ)を論点の数で等分し、論点のターンを優先する。章全体が 800 字に収まれば全文
- 理由: 引用タグは本文に機械的に付いており照合が確実。実台本(20261006)では論点が章の前半に固まり、窓をまとめて先頭から詰める案だと後半の論点が入らなかった
- 却下案: key_claim の文と本文の文字列照合(言い換えで外れる)/ 重なった窓を 1 つにまとめて先頭から詰める(前半に偏る)
- 補足: 前置きの「no faces」は既存テスト(変えない制約)が要求するので残し、「screen interfaces」「lettering」を外した

## 2026-10-24 概要欄の出典は「📄 タイトル(ドメイン名、公開日)」の 1 行にし、URL を載せない(ブリーフ 008)
- 決定: `metadata_builder.build_video_description` の参考文献を 1 件 1 行の「📄 タイトル(ドメイン名、公開日)」にする(www. は外す、タイトル無しは「参考文献N(ドメイン)」)。組み立ての最後と `_upload_to_youtube` のアップロード直前で `strip_urls` を通し、残った URL は落として続行する。metadata.txt の参考文献・「概要欄用テキスト」も同じく URL を外す
- 理由: 2026-10-23 の回が概要欄の出典リンク先ドメインの分類で削除された。URL が無ければ 3 行構造の空行区切りは不要。保険を 2 か所に置けば概要文(radio_director 由来)・フッターの URL も止まる
- 却下案: 🔗 行だけ消して「タイトル + 空行」を残す(間延びする)/ 出典節ごと消す(出典を示す目的に反する)
- 補足: 旧書式(🔗 URL 行)を要求していた既存テスト `test_metadata_description_format.py` は、仕様変更に伴う期待値の更新として差し替えた(2026-10-24 PM 判断)。ドメイン名の表記もそのままで PM 了承

## 2026-10-24 場面は章のタイムライン項目の中に持たせる(ブリーフ 009)
- 決定: `SegmentTimelineEntry.scenes`(場面の画像・開始ターン・表示時刻)を足し、`timeline.segments` は章単位のまま残す。映像トラックだけ `VideoTimeline.video_scenes()` で場面の順に並べる。場面の時刻は章の境界を決めた後(`align_cuts_to_segment_starts` の後)に `assign_scene_times` で章の映像の範囲に敷き詰める
- 理由: 章タイトル・チャプター・ジングル(音声トラック)・既存テストは章単位のまま動き、章の境界の正しさ(005)を壊さない。場面の切れ目は必ずターンの頭なので章タイトルと食い違わない
- 却下案: `timeline.segments` を場面の列に置き換える(ジングル・章の境界のテストと音声トラックが章単位を前提にしている)
- 補足: 場面の画像プロンプトは、章をその場面のターンだけに絞ったコピー(`deep_dive_1_scene2`)を従来の `get_image_for_segment` に渡して作る。007 の抜粋の仕組みがそのまま場面の範囲に効く

## 2026-10-24 パンは scale + crop の x を t の式で動かし、映像トラックの NVENC は別の設定にする(ブリーフ 009)
- 決定: 各場面を 1.05 倍に拡大し、`crop=1920:1080:x=96*t/D` で画面幅の 5% を場面の長さで動かす(左右交互、縦は中央固定、ズームなし)。画像入力に `-framerate 30` を付ける。映像トラックの NVENC は新しい `video_track_use_gpu`(既定 false)で切り替える
- 理由: 006 の案どおり zoompan より軽く、`-loop 1` の入力のまま使える。式にカンマを使わないのでフィルタグラフのエスケープが要らない。`use_gpu` は本番で true のため、流用すると映像トラックの既定が変わってしまう
- 却下案: zoompan(重い・揺れやすい)/ `use_gpu` を映像トラックにも効かせる(既定を libx264 に保てない)
- 補足: 試しエンコード(1 分)はパンでサイズ 1.1 倍・時間ほぼ同じ。2 倍を超えないので NVENC は必須ではない。既定エンコーダは libx264 ultrafast のまま(2026-10-24 PM 判断)
