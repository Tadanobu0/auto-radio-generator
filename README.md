# 🎙️ 自動ラジオ動画生成システム v4.0.0 (Step 4 v2)

VOICEVOX + FFmpeg + **FLUX.1** + **外部台本モード（Mac 側 `radio_director` 連携）** で
動画生成を行うプロジェクトです。Step 4 v2 (2026-05-10) で **Gemini 自動台本生成経路を物理削除**し、
外部台本モード（VerifiedScript JSON 受け取り）を唯一の自動経路に確定しました。

## 🆕 Step 4 v2 (2026-05-10) - Gemini 自動経路の物理削除

外部台本モード (`--phase external`) を Generator タブの自動経路として確定。Perplexity リサーチ
ベンチマーク経路と HITL タブは保持（@deprecated 注記付き）。

### 削除済み機能
- **Gemini 直叩き台本生成**: `services/script_generation/gemini_client.py` 物理削除
- **Gemini Adapter**: `services/script_generation/adapters/gemini_adapter.py` 物理削除
- **GeminiUsage 後方互換 alias**: `core/models/usage.py` から削除（`LLMUsage` に統一）
- **CLI: `--phase all` / `--phase script`**: `main.py` から削除（外部台本モードに集約）
- **CLI: `--provider` / `--research-brief`**: 不要となったため削除
- **UI: 旧 LLM 経路アコーディオン**: app.py から物理削除
- **UI: llm_provider / second_mode / jingle_dropdown**: 物理削除
- **workflow.py: `generate_video_workflow` / `_execute_gradio_scripting_phase` /
  `execute_planning_phase`**: Phase 1+2 関連関数を物理削除
- **workflow.py: 2-Story Mode**: `_merge_scripts` / `_create_script_full_transcript` 削除

### 保持された機能（Yuru-Stoic 方針）
- **外部台本モード**: 推奨経路（`--phase external --verified-script <path>` / Gradio UI）
- **Perplexity リサーチ単独実行**: `--phase research` ベンチマーク用（AI 検索計画作成は廃止し
  `queries=[theme]` のシンプル経路に変更）
- **HITL タブ**: 完全動作。`services/pipeline/scripting_phase.py` + `orchestrator.py` +
  agents（topic_curator / segment_generator / metadata_generator / fact_extractor /
  fact_checker / show_runner）を @deprecated 注記付きで保持
- **provider-agnostic adapter**: OpenAI / Anthropic / Ollama を保持（@deprecated 注記）

### 推奨経路
```bash
# 外部台本モード（Mac 側 radio_director の VerifiedScript JSON）
python main.py --phase external --verified-script output/imports/run_001/verified_script.json

# Perplexity リサーチ単独実行（ベンチマーク用）
python main.py --phase research --theme "テスト" --mode trivia

# 既存セッションから動画生成のみ
python main.py --phase render --session 20260404_065500
```

詳細: `docs/step4_implementation_plan.md`

---

## 🔧 過去の修正（Unreleased / Step 3 まで）

### 動画品質の改善
- ✅ **動画途切れ問題の修正**: 末尾5秒が欠落する問題を解決（音声497.6秒 → 動画497.6秒に一致）
- ✅ **FLUX.1タイムアウト対策**: 低VRAM環境向けに設定を最適化（処理時間 211秒 → 50-60秒）
  - タイムアウト: 120秒 → 300秒
  - 推論ステップ: 20 → 10
  - 解像度: 1344×768 → 1024×576
- ✅ **フォールバック機能の強化**: FLUX.1失敗時に静的背景画像へ自動切り替え

### コード品質の向上
- ✅ **Visual Identity リファクタリング完了**: 型安全性の向上とアーキテクチャのクリーンアップ
  - `VisualPalette` を Type Alias に変更し、`isinstance()` チェックを不要に
  - デフォルト値を定数化し、複数箇所での不整合を防止
  - `to_prompt_fragment()` が色+aestheticを返すように修正（真の後方互換性）
  - 冗長なパラメータを削除し、インターフェースを簡素化
  - すべての型アノテーションを `Optional[VisualIdentity]` に統一
- ✅ **データの不変性確保**: Phase 2.5を廃止し、適切な非同期コンテキストで処理
- ✅ **DRY原則の適用**: 重複コードを定数化し保守性を向上

## 📋 Project Overview

- **目的**: YouTube/Podcast向けラジオ動画（MP4）の制作工数を削減し、品質を安定化する
- **入出力**: テーマ入力から `script.json` / `metadata.txt` / `thumbnail.png` / `video.mp4` を生成
- **実行形態**: Gradio Web UI (`app.py`) と CLI (`main.py`) の両対応
- **ワークフロー中心**: `workflow.py` が各サービスを統合して処理順序を管理

## 📋 Current Features

### コア機能
- **リサーチ**: Perplexity API でテーマを事前調査（5モード対応）
- **台本生成**: **複数LLMプロバイダー対応** - Gemini / OpenAI / Anthropic から選択可能
  - **Gemini**: 高品質な台本生成（デフォルト）
  - **OpenAI**: Structured Outputs による確実なJSON出力（gpt-4o-mini / gpt-4o）
  - **Anthropic**: Tool Calling による構造化出力（Claude 3.5 Sonnet）
- **音声合成**: VOICEVOX で2人のパーソナリティの音声を合成
- **字幕生成**: ASS形式の字幕ファイルを自動生成（ワイドスクリーン最適化）
- **動画生成**: FFmpeg で背景画像・音声・BGM・字幕を合成
- **サムネイル生成**: センターセーフ方式で1:1トリミング対応
- **YouTubeチャプター**: 自動生成されたセクションマーカーでチャプター対応
- **概要欄の堅牢化**:
  - YouTube向けサニタイズ（制御文字除去、Unicode正規化、5000文字制限）
  - 参考文献の3行構造（📄タイトル / 🔗URL / 空行）
  - セクション間余白ルール（見出し前2行、見出し後0〜1行）
  - エンコーディング耐性のあるWebタイトル取得（`chardet`）

### v3.6.0 新機能（Direct Regex Bypass Pipeline）
- ⚡ **2段階生成のパフォーマンス最適化**: ローカルLLM向けの高速化
  - **Direct Regex Bypass**: Ollama等のローカルLLM使用時、Phase 2のLLM呼び出しをスキップ
  - **ハイブリッドパイプライン**: LLM（クリエイティブ生成）+ Python（正規表現構造化）
  - **処理時間35-40%短縮**: 43分 → 25-28分（API呼び出し半減）
  - **JSON生成100%成功**: 正規表現パーサーによる確実な変換
  - **プロバイダー別最適化**: クラウドLLMは従来通りPhase 2を実行
  - **設定**: `config.yaml > orchestrator.two_phase_generation: true`

### v3.5.0 新機能（Hierarchical Agentic Workflow）
- 🎯 **長尺台本生成アーキテクチャ**: 高密度リサーチデータから150ターン超の台本を安定生成
  - **TopicCurator**: リサーチデータから「意外性・具体性・議論性」の3軸で2〜3トピックを厳選
  - **SegmentGenerator**: intro / deep_dive / conclusion を独立したAPI呼び出しで生成（**2段階生成対応**）
  - **ScriptOrchestrator**: 全体統括・文脈管理・セグメント統合を担当
  - **文脈の連続性**: 各セグメントの `context_summary` を次セグメントに引き継ぎ
  - **無限のスケーラビリティ**: セグメント単位で生成するため `max_output_tokens` の壁を回避
  - **フィーチャーフラグ**: `config.yaml > orchestrator.enabled` で新旧を切り替え可能（デフォルト: false）
  - **コスト最適化**: キュレーションは軽量モデル（gemini-2.5-flash）を使用
  - **進捗フィードバック**: 各セグメント生成の進捗をリアルタイム表示
  - **リトライ処理**: セグメント単位で最大2回リトライ、部分失敗にも対応
  - **JSON切断問題の堅牢化**: Unterminated string エラーを防止
    - `max_output_tokens` を十分に確保（TopicCurator: 8192, MetadataGenerator: 4096）
    - `response_mime_type: "application/json"` を使用しない（切断の原因となるため）
    - `finish_reason=MAX_TOKENS` を検出して警告
    - 4段階のサニタイズ処理（コードブロック除去、JSON抽出、制御文字除去、空白除去）
    - エラー時は完全な生レスポンステキストをログ出力（デバッグ用）
    - MetadataGeneratorはnon-fatalでフォールバック動作
  - **詳細ドキュメント**: `docs/script_generation_current_state.md` に現状分析を記載

### v3.4.0 新機能（Multi-LLM Provider Support）
- 🤖 **複数LLMプロバイダー対応**: Gemini / OpenAI / Anthropic から台本生成エンジンを選択可能
  - **ファクトリーパターン**: プロバイダー名から適切なクライアントを自動生成
  - **OpenAI Structured Outputs**: `client.beta.chat.completions.parse()` でJSON構造を保証
  - **Anthropic Tool Calling**: ツール定義による構造化出力の強制
  - **UI統合**: Gradio UIのドロップダウンで3プロバイダーを動的切り替え
  - **後方互換性**: デフォルトは `gemini`、既存機能は無変更で動作
  - **設定ファイル拡張**: `config.yaml` に各プロバイダーのモデル設定を追加
  - **環境変数**: `.env` に `OPENAI_API_KEY` と `ANTHROPIC_API_KEY` を追加

### v3.3.0 新機能（Negative Prompt / Audio Pro / Quality）
- 🧭 **Manually Controlled Topics**: 「避けてほしい話題」(Negative Prompt) による生成内容の制御
  - UI上で除外トピックを自由入力し、Geminiプロンプトに自動反映
  - スペースやカンマ区切りで複数トピック指定可能
- 🔊 **Pro-Level Audio**: ラウドネスノーマライゼーション (-14 LUFS) による音圧自動調整
  - YouTube推奨基準 (I=-14, TP=-1, LRA=11) に準拠
  - FFmpeg `loudnorm` フィルタで音声ミキシング後に自動適用
- 📊 **Visual Progress**: 詳細な進捗バー表示
  - Gradio進捗バーで各フェーズの状況をリアルタイム表示
  - 🤔 企画 → 🔍 リサーチ → 📝 台本 → 🗣️ 音声 → 🎬 動画 → ✨ 完了
- 🚀 **High Performance**: Mockモード開発とNVENC (GPU) レンダリング
  - NVIDIA GPUによるハードウェアエンコード対応（h264_nvenc）
  - API課金なしでワークフロー全体をテスト可能なMockモード
  - Settingsタブの **Developer Options >「🧪 モックで動画を作成」** から実行可能
  - Mock実行時はテーマ未入力でも動作（内部でダミーテーマを補完）

### v3.3.2 安定化向上（第2部モード・API接続）
- 🎭 **第2部モード**: 1つのテーマで2部構成のラジオ番組を生成可能
  - 第1部の内容を踏まえた第2部の深掘り（重複回避）
  - ジングル音声による場面転換
- 🔧 **APIヘルスチェック**: 生成前にGemini/Perplexity接続状態を確認
  - UI上で手動実行可能な接続テスト機能
  - エラー時に詳細な原因表示（認証・制限・接続）
- 🔄 **話者逆転自動補正**: 第2部での声割当ミスを自動検出・修正
  - 口調パターン（「なのだ」「わよ/かしら」）で逆転を検出
  - 自動でA/B話者を入れ替え、音声合成前に修正
- 🌐 **リトライ処理**: API接続エラー時に自動再試行（最大2回）
  - 接続断・タイムアウト・サーバーエラーを対象
  - 指数バックオフで1秒→2秒と待機時間を延長
- 🌡️ **温度制御**: 第2部モードでtemperatureを0.7に下げ、JSON安定性向上
  - 第2部の巨大入力によるJSONパースエラーを防止
  - 通常時は0.85を維持し創造性を確保

### v3.3.1 安全強化（Perplexity 呼び出し制御）
- 🛡️ **Perplexity 呼び出しハードリミット**: 1ワークフローあたりのAPI呼び出し数を制限
  - `max_requests_per_workflow` で上限設定（デフォルト: 6件）
  - 上限超過時は即時停止し、コスト暴走を防止
- 🔄 **同一セッションキャッシュ**: 同一クエリのリサーチ結果をメモリ内で再利用
  - `enable_session_cache` で有効化（デフォルト: true）
  - API呼び出し回数を削減し、応答速度を向上
- 📏 **クエリ数上限**: 企画フェーズで生成される検索クエリ数を制限
  - `max_queries_per_plan` で上限設定（デフォルト: 3件）
  - 超過クエリは自動的にトリムされ、予期せぬ呼び出しを防止
- 📊 **実行ログ拡張**: `execution_record_*.jsonl` に Perplexity 実リクエスト数を記録
  - 失敗時含む全実行で正確な呼び出し回数を記録
  - コスト管理と監査の精度を向上

### v3.3.x 運用強化（Topic Overlay / Mock Operation）
- 🏷️ **Topic Overlay**: チャプター開始行（`section` + `chapter_title`）をもとに、動画上部へ話題ラベルをオーバーレイ表示
  - 設定キー: `config.yaml > video.show_topic_overlay`
  - 目的: 視聴者が現在の話題を見失わないようにする
- 🧪 **Mock運用の明確化**: 開発検証は Settings タブの **「🧪 モックで動画を作成」** を使用
  - Mock実行時は API 呼び出しを行わず `tests/mock_data/` を利用
  - Mock実行時は YouTube アップロードを実行時に自動無効化

### v3.3.0 メンテナンス更新（UI Modularization / Logging）
- 🧩 **UI Modularization**: `app.py` の巨大UIをタブ単位関数へ分割
  - `create_generator_tab()` / `create_dashboard_tab()` / `create_settings_tab()` / `create_manual_tab()`
  - イベント配線を `create_ui()` 下部へ集約し、UI定義と動作ロジックを分離
- 📈 **Dashboard**: 実行履歴とコスト履歴の可視化タブを追加
  - 月次切り替え、実行テーブル、コスト推移、モデル別利用率を表示
- 🗂 **Append-Only Logging基盤**: JSONLログを標準化
  - `logs/execution_record_YYYY-MM.jsonl`
  - `logs/cost_history_YYYY-MM.jsonl`
- 🔐 **Mock Safety Guard**: Mockモード時はYouTubeアップロードを強制無効化

### v3.1.2 以前の機能（GPU / Mock / UI強化）
- 🛡️ **後方互換性強化**: 旧JSON形式（`speaker_id`/`dialogue`）の自動変換バリデータ追加

### v3.1.1 新機能（Structured Script Output）
- 🔧 **Structured Output Stability**: Gemini出力の構造化安定性を強化
  - 構造化スキーマ準拠の出力で後段処理の堅牢性を向上
  - 解析失敗時の保険ロジックを簡素化し保守性を改善

### v3.1 新機能（Phase 2）
- 🏷 **Automated Metadata**: タイトル・概要・サムネイル文字の完全自動生成（チャプター付き）
  - AI生成タイトルに制作日を自動付与
  - 概要欄の冒頭にチャプターリストを配置
  - サムネイル中央にAI生成キャッチフレーズ、右上に日付バッジ
- 📜 **High-Density Script**: 50フレーズ以上の深掘り台本生成ロジック
  - リスナーメールセクションの充実化
  - セクション間の自然な接続強化
- 🎨 **Modern UI**: カード型レイアウトによる直感的な操作画面
  - タブ式インターフェースで自動生成とマニュアル制作を分離
  - 設定の永続化とワンクリック復元
- 🛡️ **Stability**: 静的解析に基づく堅牢なエラー対策
  - JSON解析の改善（マークダウンブロック対応）
  - テーマ引き継ぎロジックの修正

### v3.0 新機能（Phase 1）
- **タブ式UI**: 自動生成とマニュアル制作を分離した直感的なインターフェース
- **マニュアル制作ワークフロー**: 3ステップで完結する柔軟な制作フロー
  - **Step A: 台本生成** - リサーチ結果から台本を生成
  - **Step B: 音声合成** - 台本を編集して音声を合成
  - **Step C: 動画レンダリング** - 背景画像とBGMを追加して動画を生成
- **設定の永続化**: ユーザー設定を自動保存・復元
- **処理ログ出力**: 各実行の詳細ログをファイルに保存
- **自動データ連携**: Step A→B→Cで生成物が自動的に次のステップに反映

### v2.1 機能
- **週次ニュースダイジェスト**: 直近1週間の重要ニュースをトップ3選定
- **Markdownレポート**: リサーチ結果を構造化された読みやすい形式で保存
- **センターセーフサムネイル**: Podcast配信時の正方形トリミングに対応
- **限界設定字幕**: 画面端ギリギリまで拡大した超大型字幕（モバイル最適化）

### v2.0 機能
- **Web UI**: Gradio ベースのブラウザ操作画面
- **リサーチモード**: ディベート / 世間の声 / トリビア / 週次ダイジェスト / 解説・講座
- **多様な台本形式**: 
  - 3部構成（本題/リスナーメール/エンディング）
  - ニュースダイジェスト（トップ3形式）
  - 講座形式（先生と生徒の対話）
- **音声スペクトラム**: 画面下部に音声波形を表示

## 🚀 セットアップ

### システム要件 (Requirements)

| ツール | バージョン | 備考 |
|--------|-----------|------|
| **Python** | 3.10+ | 必須 |
| **FFmpeg** | 6.0+ | NVENC対応ビルド推奨（GPU高速化に必要） |
| **VOICEVOX Engine** | 0.24+ | GPU版推奨（音声合成の高速化） |

Pythonパッケージは `requirements.txt` で管理しています（主な依存: `google-genai`, `openai`, `anthropic`, `gradio`, `requests`, `beautifulsoup4`, `chardet`, `pytest`）。

> **Pythonバージョン運用方針（メンテナンス注記）**  
> 現在は **Python 3.10.6** で安定動作しており、**2026年10月（サポート期限）までは現行環境を維持**します。開発効率を優先しつつ、
> Google API ライブラリ（`google-api-core` 系）の Python 3.10 サポート終了（**2026-10-04**）に伴い、
> 将来的には **Python 3.11 以上** への移行が必要です。

### 1. 依存パッケージのインストール

```bash
cd auto_radio_generator
pip install -r requirements.txt
```

補足:
- `workflow.py` は企画・リサーチ・台本・音声・動画・公開の各フェーズを統合するオーケストレーターです。
- `app.py` は UI 定義とイベント配線を担当し、直接 `launch()` されるのは `if __name__ == "__main__":` 実行時のみです。

### 2. 環境変数の設定

`.env.example` を `.env` にコピーし、APIキーを設定：

```bash
copy .env.example .env
```

```env
PERPLEXITY_API_KEY=pplx-xxxxxxxx  # Perplexity使用時
GEMINI_API_KEY=AIzaSyxxxxxxxx     # Gemini使用時（デフォルト）
OPENAI_API_KEY=sk-xxxxxxxx        # OpenAI使用時（オプション）
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx # Anthropic使用時（オプション）
VOICEVOX_BASE_URL=http://localhost:50021
```

### 3. アセットの配置

以下のファイルを配置してください：

- `assets/backgrounds/default.png` - 背景画像 (1920x1080推奨)
- `assets/bgm/default.mp3` - BGM音楽ファイル

### 4. VOICEVOXエンジンの起動

```bash
# VOICEVOXエンジンを起動
..\voicevox_engine-windows-nvidia-0.24.0\windows-nvidia\run.exe
```

### 5. 実行

**Web UI（推奨）**
```bash
python app.py
```
ブラウザで http://127.0.0.1:7861 を開きます。

Mock実行は、Settingsタブの **Developer Options** にある
**「🧪 モックで動画を作成」** ボタンを利用してください。

**CLI版（パイプライン分離対応）**

```bash
# 一気通貫モード（全フェーズ実行）
python main.py --theme "持続血糖測定器CGMについて" --mode lecture

# フェーズ単位での実行
# 1. リサーチフェーズのみ実行
python main.py --phase research --theme "持続血糖測定器CGMについて" --mode lecture

# 2. 台本作成フェーズのみ実行（既存セッション使用）
python main.py --phase script --session 20260404_065500

# 3. 動画生成フェーズのみ実行（既存セッション使用）
python main.py --phase render --session 20260404_065500

# 外部ファイルから読み込んで実行
python main.py --phase script --research-brief workspace/20260404_065500/research_brief.json
python main.py --phase render --script workspace/20260404_065500/script_artifact.json

# 🎬 外部台本モード（推奨、Step 3 / 2026-05-09 追加）
# Mac 側 radio_director の VerifiedScript JSON で動画生成のみを実行。LLM コスト ¥0
python main.py --phase external --verified-script output/imports/run_001/verified_script.json
```

**パイプライン分離の利点**:
- **フェーズ単位のデバッグ**: 失敗したフェーズのみを再実行可能
- **コスト削減**: 全体を再実行せず、特定フェーズのみを修正・再実行
- **柔軟な開発**: 各フェーズを独立して改善・テスト可能
- **セッション管理**: `workspace/{session_id}/` 配下に中間成果物を永続化

## 🎬 外部台本モード (Step 3 / 2026-05-09)

Mac Studio 側 `radio_director` パイプラインが生成する **VerifiedScript JSON** を
入力として、Phase 1 (リサーチ計画) と Phase 2 (台本生成) を**完全 bypass** し、
動画生成 (VOICEVOX → FFmpeg → NVENC) のみを実行する経路。

### 設計コンセプト

- **SSOT 分離**: Mac 側で台本品質を担保 → Windows 側は動画化に専念
- **コスト ¥0**: Gemini / Perplexity API は一切呼ばれない (旧 LLM 経路の `@deprecated` 警告で識別可能)
- **手動レビュー**: VerifiedScript JSON を Mac から手動コピーすることで人間レビュー工程を確保

### 使い方

#### Gradio UI から
1. `output/imports/<run_id>/verified_script.json` に Mac 側 VerifiedScript を配置
2. Generator タブの「🎬 外部台本モード（推奨 / VerifiedScript JSON）」アコーディオンを展開
3. ファイルピッカーで JSON を選択
4. 「動画を生成する」ボタンで実行

#### CLI から
```bash
python main.py --phase external --verified-script output/imports/run_001/verified_script.json
```

### VerifiedScript JSON の構造

Mac 側 `radio_director` の SSOT 仕様 (詳細は `docs/step3_implementation_plan.md` 参照):

```json
{
  "script": {
    "show_spec": { ... },
    "segments": [
      {"segment_type": "intro", "title": "...", "turns": [{"speaker": "A", "text": "..."}, ...]},
      {"segment_type": "deep_dive", "topic_index": 0, "title": "...", "turns": [...]},
      ...,
      {"segment_type": "conclusion", "title": "...", "turns": [...]}
    ],
    "metrics": { ... }
  },
  "metrics": { ... },
  "warnings": [ ... ],
  "metadata": {
    "title": "動画タイトル",
    "thumbnail_title": "短縮 (15字以内)",
    "description": "概要文 (50〜2000字)",
    "hashtags": ["#タグ1", "#タグ2", "#タグ3"],
    "chapters": [{"timestamp": "0:00", "title": "イントロ"}, ...],
    "references": []
  }
}
```

### Step 4 (v2) で削除予定の項目

外部台本モードでの動画生成が連続 10 回以上フォールバック発動なしで成功した時点で、
以下を物理削除する:

- `services/research/` (PerplexityResearcher、Step 3 では `@deprecated`)
- `services/script_generation/` のうち Gemini 経路 (GeminiClient.generate)
- `services/cost_calculator.py`
- `core/interfaces/script_generator.py`、`researcher.py`
- `core/models/usage.py` の `GeminiUsage` / `PerplexityUsage` 部分
- 旧 Generator タブ UI (theme/research_mode/avoid_topics 入力欄、Deprecated アコーディオン全体)
- `main.py` の旧 phase 経路 (`--phase research/script/all`)
- `run_workflow_sync` の theme 引数など旧経路向けパラメータ

## 🔑 YouTube API のセットアップ

YouTube への自動アップロード機能を使う場合、プロジェクトルートに `client_secret.json` が必要です。

### 必要ファイル

- `client_secret.json`（OAuth クライアント情報）
- `client_secret.json.example`（ダミー構成テンプレート、詳細はこのファイルを参照）

### 取得方法（Google Cloud Console）

1. Google Cloud Console でプロジェクトを作成
2. **YouTube Data API v3** を有効化
3. 「認証情報」から **OAuth クライアント ID** を作成（アプリケーション種別: **デスクトップアプリ**）
4. ダウンロードした JSON をプロジェクトルートに `client_secret.json` として配置

### セキュリティ上の注意（重要）

- `client_secret.json` と `token.json` は **絶対にリポジトリへコミットしないでください**。
- 本リポジトリでは `.gitignore` で除外設定済みです。
- 構成の参考が必要な場合は `client_secret.json.example` を使用してください（ダミー値のみ）。

## 🧪 開発・テスト (Development & Testing)

### 手動テスト

プロジェクトルートで以下を実行すると、単体テストを手動実行できます。

```bash
# 全テスト実行
pytest

# サムネイル再作成機能テストのみ
python -m pytest tests/test_thumbnail_regeneration.py -v

# カバレッジ付きで実行
python -m pytest tests/ --cov=. --cov-report=html
```

### 自動チェック（pre-commit hook）

`git commit` 実行時に `.git/hooks/pre-commit` が自動で **軽量な構文チェック** を実行します。

- 実行コマンド: `python -m py_compile`（リポジトリ内の `.py` を対象）
- 目的: コミット速度を維持しつつ、構文エラーの混入を防ぐ
- 方針: **重い全体テストは手動実行**（必要時に `pytest -v -s`）

- 構文エラー時: `Syntax check failed ... Commit rejected.` を表示し、コミットを中止
- 正常時: `Syntax check passed ...` を表示し、コミットを許可

### フック再生成手順（clone直後）

`.git/hooks/` は通常リポジトリ管理対象外のため、clone 直後は pre-commit hook を再配置してください。

```bash
# プロジェクトルートで実行
copy hooks\pre-commit .git\hooks\pre-commit
```

Git Bash を使う場合:

```bash
cp hooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

※ `hooks/pre-commit` はリポジトリ管理されるフック実体です。clone 後に `.git/hooks/pre-commit` へコピーして利用してください。

### 現在のテスト範囲

#### サムネイル再作成機能テスト（新規追加）
- `tests/test_thumbnail_regeneration.py` - サムネイル再作成とState管理の自動テスト
  - `TestThumbnailRegenerationState`: Stateデータクラスの基本機能
  - `TestGenerateVideoMock`: generate_video_mock の戻り値と呼び出し引数
  - `TestThumbnailRegeneration`: サムネイル再作成の成功・失敗ケース
  - 実行コマンド: `python -m pytest tests/test_thumbnail_regeneration.py -v`

#### 既存テスト
現時点では、`services/video_rendering/ffmpeg_renderer.py` の Windows パス変換ロジック（例: 字幕パスのエスケープ処理）を中心に単体テストを整備しています。

主な対象:
- `_escape_windows_path()` のパス変換（`\\` → `/`, `:` → `\\:`）
- スペース・日本語・UNC パスなどのエッジケース

## 🔭 今後の展望 (Future Outlook)

- **アイキャッチ動画によるブランド力強化**: セグメント間に専用アイキャッチ（Stinger動画）を導入し、番組の視覚的統一感とプロフェッショナル感を向上させる。
  - **技術的アプローチ**: FFmpegでの重いトランジション処理（静止画同士のxfade）は使用せず、あらかじめフェードイン・フェードアウト等のアニメーションが適用された「2〜3秒の短い動画ファイル（.mp4等のStinger動画）」を用意し、既存のシンプルで軽量な `concat` フィルタを用いて結合する。
  - **期待効果**: レンダリング負荷を上げずにリッチなトランジションを実現し、視聴者の没入感とチャンネルブランドの認知度を向上させる。
  - **実装ヒント**: `assets/stingers/` ディレクトリに複数のStinger動画を配置し、`TimelineCalculator` でセグメント境界にStinger挿入タイミングを計算、`FFmpegRenderer` の `concat` フィルタで結合する構成を想定。

- **視聴者エンゲージメントの自動化**: 動画公開と同時に、参考文献や台本要約などの補足情報をコメント欄へ自動提供し、視聴者の利便性と信頼性を向上させる。
- **メタデータの高度化**: タイムスタンプとリッチな概要欄により、視聴者の利便性を最大化し、YouTube 検索におけるプレゼンスを強化する。
- **情報の透明性**: 参考文献の自動掲載により、AI 生成コンテンツとしての信頼性と価値を高める。
- **設計ヒント**: `YouTubeClient` に `insert_comment(video_id, text)` のようなメソッドを追加し、`workflow.py` の最終工程で呼び出す構成を想定。
- **実装ヒント（チャプター概要欄）**: `workflow.py` の動画生成フェーズで各パートの累積時間を計算し、チャプター付き説明文を `YouTubeClient.upload_video(..., description=...)` に渡す構成を想定。

## ⚙️ 設定

`config.yaml` で以下の設定が可能：

| 設定項目 | 説明 |
|---------|------|
| `researcher.model` | リサーチ用モデル (デフォルト: sonar-reasoning-pro) |
| `researcher.max_queries_per_plan` | 1回の企画で採用する検索クエリ上限 (デフォルト: 3) |
| `researcher.max_requests_per_workflow` | 1ワークフロー内で許可するPerplexity呼び出し上限 (デフォルト: 6) |
| `researcher.enable_session_cache` | 同一セッション内で同一クエリのリサーチ結果を再利用 (デフォルト: true) |
| `researcher.modes` | リサーチモード定義 (debate/voices/trivia/weekly_digest/lecture) |
| `script_generator.default_provider` | デフォルトLLMプロバイダー (デフォルト: gemini) |
| `script_generator.gemini.model` | Gemini台本生成用モデル (デフォルト: gemini-3.1-pro-preview) |
| `script_generator.gemini.fallback_model` | Geminiフォールバックモデル (デフォルト: gemini-2.5-pro) |
| `script_generator.openai.model` | OpenAI台本生成用モデル (デフォルト: gpt-4o-mini) |
| `script_generator.openai.fallback_model` | OpenAIフォールバックモデル (デフォルト: gpt-4o) |
| `script_generator.anthropic.model` | Anthropic台本生成用モデル (デフォルト: claude-sonnet-4-6) |
| `script_generator.structure` | 台本構成比率 (本題/メール/エンディング) |
| `audio_synthesizer.speakers.main` | メインパーソナリティのVOICEVOX話者ID |
| `audio_synthesizer.speakers.sub` | サブパーソナリティのVOICEVOX話者ID |
| `audio_synthesizer.speed_scale` | 話速 (1.0=標準、1.1=やや速め) |
| `video_renderer.bgm_volume` | BGM音量 (0.0〜1.0) |
| `video_renderer.enable_spectrum` | 音声スペクトラム表示 |
| `personalities` | 各パーソナリティの名前・性格設定 |

### 画像生成(Forge / ComfyUI / B70)

- 送り先は `image_provider`(`forge` / `comfyui`)と、`flux:` / `comfyui:` の節で決まる。リポジトリの config.yaml は Windows の本番用(127.0.0.1)
- **config.yaml に重ねる yaml**(2026-11-08、ブリーフ 022): 環境変数 `ARG_CONFIG=<yaml>` か `run_workflow_sync(..., config_overlay="<yaml>")`。相対パスは ARG の直下から。書いたキーだけが置き換わる(dict はキーごと)。ファイルが無い・壊れているときは止まる
- **B70 の ComfyUI へ送る**: `config/b70.overlay.yaml`(送り先 `http://192.168.0.70:8188`、sampler euler / scheduler simple / steps 4 / cfg 1、ポーリング 0.5 秒)。width / height は本番に合わせて書き換える前提
- **B70 のワークフロー**: `config/workflow_b70_flux1_schnell_fp8.api.json` は写し。**正本は b70-box の `comfyui/workflows/flux1-schnell-fp8.api.json`**。正本を変えたらこちらも写し直す。ARG はプロンプト・大きさ・seed・steps・cfg・sampler・scheduler を毎回書き換えるので、写しに入っている値は使われない
- ComfyUIClient はノードを決め打ちの番号(3/4/5/6/7/9、`comfyui.node_ids` で上書き可)で探し、合わなければ class_type と KSampler の配線で探す。潜在画像は `EmptyLatentImage` と `EmptySD3LatentImage` のどちらでもよい
- 画像のキャッシュ(`output/.image_cache/`)のキーには、種類・送り先・大きさが入る。127.0.0.1 / localhost の送り先だけ、前の形(プロンプトだけ)のファイルも見る

### リサーチモード

| モード | 説明 | 用途 |
|--------|------|------|
| ディベート | 賛成・反対の両論を調査 | 議論形式の番組 |
| 世間の声 | SNSの反応や一般意見を収集 | カジュアルな雑談番組 |
| トリビア | あまり知られていない事実を調査 | 雑学・豆知識番組 |
| 今週のまとめ | 直近1週間のニュースをトップ3選定 | ニュースダイジェスト |
| 解説・講座 | 初心者向けに比喩を使って解説 | 教育・学習番組 |

### VOICEVOX話者ID例

| ID | キャラクター |
|----|-------------|
| 1 | 四国めたん（ノーマル） |
| 2 | 四国めたん（あまあま） |
| 3 | ずんだもん（ノーマル） |
| 8 | 春日部つむぎ |

## 📁 出力ファイル

### 自動生成モード
生成されたファイルは `output/YYYYMMDD_HHMMSS/` に保存されます：

```
output/
└── 20241221_143000/
    ├── audio/
    │   ├── combined_audio.wav   # 合成音声
    │   └── subtitles.ass        # 字幕ファイル（ASS形式）
    ├── videos/
    │   └── radio_20241221_143000.mp4  # 完成動画
    ├── research.json            # リサーチ結果（JSON形式）
    ├── research_report.md       # リサーチレポート（Markdown形式）
    ├── script.json              # 生成された台本
    ├── metadata.txt             # YouTube投稿用メタデータ
    ├── thumbnail.png            # サムネイル画像（1280x720、センターセーフ対応）
    └── processing_log.txt       # 処理ログ
```

### マニュアル制作モード
マニュアル制作の出力は `output/manual_builds/YYYYMMDD_HHMMSS/` に保存されます：

```
output/
└── manual_builds/
    └── 20241221_143000/
        ├── combined_audio.wav   # Step B: 合成音声
        ├── subtitles.ass        # Step B: 字幕ファイル
        └── video.mp4            # Step C: 完成動画
```

## 🚀 新機能・改善

### v3.3.2 以降の主要更新
- **第2部モード対応**: 1つのテーマで2部構成のラジオ番組を生成可能
  - 第1部の内容を踏まえた第2部の深掘り（重複回避）
  - ジングル音声による場面転換
- **APIヘルスチェック**: 生成前にGemini/Perplexity接続状態を確認
- **話者逆転自動補正**: 第2部での声割当ミスを自動検出・修正
- **リトライ処理**: API接続エラー時に自動再試行（最大2回）
- **温度制御**: 第2部モードでtemperatureを0.7に下げ、JSON安定性向上

### 第2部モードの使い方
1. Generatorタブで「第2部のリサーチモード」を選択
2. 「場面転換ジングル」を選択またはカスタム音声を指定
3. 通常通りテーマを入力して生成

## 🏗️ アーキテクチャ

```
auto_radio_generator/
├── app.py                   # Web UI エントリーポイント
├── main.py                  # CLI エントリーポイント
├── workflow.py              # 共通ワークフロー
├── config.yaml              # 設定ファイル
├── requirements.txt         # Python依存パッケージ
├── user_settings.json       # ユーザー設定（自動生成）
├── logs/                    # 実行・コスト履歴（JSONL, append-only）
│   ├── execution_record_YYYY-MM.jsonl
│   └── cost_history_YYYY-MM.jsonl
├── core/                    # ドメイン層
│   ├── interfaces/          # 抽象インターフェース (ABC)
│   │   ├── researcher.py    # リサーチャーIF
│   │   ├── script_generator.py
│   │   ├── audio_synthesizer.py
│   │   └── video_renderer.py
│   ├── models/              # Pydanticモデル
│   │   ├── config.py        # 設定モデル
│   │   ├── script.py        # 台本モデル
│   │   ├── usage.py         # 使用量モデル
│   │   ├── execution_log.py # 実行ログモデル
│   │   └── cost_log.py      # コストログモデル
│   └── settings_manager.py  # 設定永続化
├── services/                # アプリケーション層
│   ├── research/            # リサーチ (Perplexity)
│   ├── script_generation/   # 台本生成 (Multi-LLM)
│   │   ├── gemini_client.py      # Gemini台本生成クライアント
│   │   ├── openai_client.py      # OpenAI台本生成クライアント (Structured Outputs)
│   │   ├── anthropic_client.py   # Anthropic台本生成クライアント (Tool Calling)
│   │   └── llm_factory.py        # LLMプロバイダーファクトリー
│   ├── audio_synthesis/     # 音声合成 (VOICEVOX)
│   ├── video_rendering/     # 動画生成 (FFmpeg)・字幕生成
│   ├── publishing/          # YouTube投稿メタデータ生成・アップロード
│   ├── media_processing/    # メディア処理
│   │   └── thumbnail_generator.py  # サムネイル生成
│   ├── cost_calculator.py   # APIコスト計算
│   └── ...                  # 補助ユーティリティ
├── config/
│   └── prompts.yaml         # リサーチ/台本生成プロンプト定義
├── assets/                  # 静的リソース
│   ├── backgrounds/         # 背景画像 (10枚以上)
│   └── bgm/                 # BGM音楽 (8曲以上)
└── output/                  # 生成物
    ├── YYYYMMDD_HHMMSS/     # 自動生成モード
    └── manual_builds/       # マニュアル制作モード
└── tests/                   # テストファイル
    ├── test_thumbnail_regeneration.py  # サムネイル再作成機能テスト
    └── test_ffmpeg_renderer.py       # FFmpegレンダラー関連テスト
```

## 🔧 拡張

インターフェース（ABC）を使用しているため、以下の拡張が容易です：

- ✅ **OpenAI対応**: `IScriptGenerator` を継承して実装済み（`openai_client.py`）
- ✅ **Anthropic対応**: `IScriptGenerator` を継承して実装済み（`anthropic_client.py`）
- **ElevenLabs対応**: `IAudioSynthesizer` を継承して実装可能
- **別レンダラー対応**: `IVideoRenderer` を継承して実装可能

### LLMプロバイダーの追加方法

新しいLLMプロバイダーを追加する場合：

1. `core/interfaces/script_generator.py` の `IScriptGenerator` を継承
2. `services/script_generation/` に新しいクライアントを実装
3. `services/script_generation/llm_factory.py` にプロバイダー追加
4. `config.yaml` に設定セクション追加
5. `.env.example` にAPIキー追加

## 🧭 開発ポリシー

- **Communication**: ユーザー向け説明は日本語
- **Code**: 関数名・変数名・コードコメント・コミットメッセージは英語
- **Config Driven**: 設定値は `config.yaml` を優先し、ハードコードを避ける
- **Input Minimal, Data Maximal**: 実行中間データは JSONL に追記保存して再利用可能にする

## 📝 ライセンス

MIT License
