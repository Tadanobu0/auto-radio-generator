"""YouTubeサムネイル画像生成サービス"""
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
from rich.console import Console
import json

from services import render_fonts

console = Console()


class ThumbnailGenerator:
    """YouTube投稿用サムネイル画像を自動生成
    
    背景画像にタイトルテキストを重ねて、視認性の高いサムネイルを作成します。
    """
    
    # YouTube推奨サイズ
    THUMBNAIL_WIDTH = 1280
    THUMBNAIL_HEIGHT = 720
    
    def __init__(self):
        """サムネイル生成器を初期化"""
        self.font_paths = self._find_japanese_fonts()
    
    def _find_japanese_fonts(self) -> list[Path]:
        """日本語フォントのパスを探索
        
        Returns:
            list[Path]: 利用可能なフォントパスのリスト（優先順）
        """
        # ARG_FONT_DIR (017): 字幕と同じ字体。題・日付バッジとも Regular (Meiryo のときと同じ太さの扱い)。
        # Pillow の size は em の px なので、字体を替えても大きさの数字はそのまま
        font = render_fonts.active_font()
        if not font.is_default:
            return [p for p in (font.regular, font.bold) if p is not None]
        candidates = [
            # Windows標準フォント
            Path("C:/Windows/Fonts/meiryo.ttc"),
            Path("C:/Windows/Fonts/msgothic.ttc"),
            Path("C:/Windows/Fonts/msmincho.ttc"),
            Path("C:/Windows/Fonts/YuGothB.ttc"),
            # FFmpegで使用しているフォント
            Path("C:/Windows/Fonts/arial.ttf"),
        ]
        
        available = [p for p in candidates if p.exists()]
        
        if not available:
            console.print("[yellow]⚠ 日本語フォントが見つかりません。デフォルトフォントを使用します。[/yellow]")
        
        return available
    
    def generate(
        self,
        title: str,
        background_path: Path,
        output_path: Path,
        thumbnail_title: str = "",
        darken_factor: float = 0.5,
        blur_radius: int = 3
    ) -> Path:
        """サムネイル画像を生成
        
        Args:
            title: 動画タイトル
            background_path: 背景画像のパス
            output_path: 出力先パス
            thumbnail_title: サムネイル用の短い釣りタイトル（なければtitleを使用）
            darken_factor: 背景を暗くする係数 (0.0-1.0, 小さいほど暗い)
            blur_radius: ブラー半径 (0で無効)
        
        Returns:
            Path: 生成されたサムネイル画像のパス
        """
        # 釣りタイトルがなければ通常のtitleを使用
        display_title = thumbnail_title if thumbnail_title else title
        
        console.print(f"[cyan]サムネイル画像を生成中...[/cyan]")
        console.print(f"  タイトル: {display_title}")
        console.print(f"  背景: {background_path.name}")
        
        # 1. 背景画像を読み込み、リサイズ
        background = self._load_and_resize_background(background_path)
        
        # 2. 背景を暗くしてブラーをかける（視認性向上）
        background = self._apply_effects(background, darken_factor, blur_radius)
        
        # 3. タイトルテキストを描画（中央にthumbnail_titleを表示）
        thumbnail = self._draw_title_text(background, display_title)
        
        # 4. 日付バッジを描画（右上に日付を表示）
        thumbnail = self._draw_date_badge(thumbnail)
        
        # 5. 保存
        output_path.parent.mkdir(parents=True, exist_ok=True)
        thumbnail.save(output_path, "PNG", quality=95)
        
        file_size_kb = output_path.stat().st_size / 1024
        console.print(f"[green]✓ サムネイル生成完了[/green] {output_path.name} ({file_size_kb:.1f} KB)")
        
        return output_path
    
    def _load_and_resize_background(self, background_path: Path) -> Image.Image:
        """背景画像を読み込み、サムネイルサイズにリサイズ
        
        Args:
            background_path: 背景画像のパス
        
        Returns:
            Image.Image: リサイズされた背景画像
        """
        img = Image.open(background_path)
        
        # アスペクト比を維持しながらリサイズ（中央クロップ）
        img_ratio = img.width / img.height
        target_ratio = self.THUMBNAIL_WIDTH / self.THUMBNAIL_HEIGHT
        
        if img_ratio > target_ratio:
            # 画像が横長 → 高さを合わせて横をクロップ
            new_height = self.THUMBNAIL_HEIGHT
            new_width = int(new_height * img_ratio)
        else:
            # 画像が縦長 → 幅を合わせて縦をクロップ
            new_width = self.THUMBNAIL_WIDTH
            new_height = int(new_width / img_ratio)
        
        img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        # 中央クロップ
        left = (new_width - self.THUMBNAIL_WIDTH) // 2
        top = (new_height - self.THUMBNAIL_HEIGHT) // 2
        right = left + self.THUMBNAIL_WIDTH
        bottom = top + self.THUMBNAIL_HEIGHT
        
        img = img.crop((left, top, right, bottom))
        
        return img
    
    def _apply_effects(
        self,
        img: Image.Image,
        darken_factor: float,
        blur_radius: int
    ) -> Image.Image:
        """背景画像にエフェクトを適用（暗くする + ブラー）
        
        Args:
            img: 元画像
            darken_factor: 暗くする係数 (0.0-1.0)
            blur_radius: ブラー半径
        
        Returns:
            Image.Image: エフェクト適用後の画像
        """
        # 暗くする
        enhancer = ImageEnhance.Brightness(img)
        img = enhancer.enhance(darken_factor)
        
        # ブラーをかける
        if blur_radius > 0:
            img = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
        
        return img
    
    def _draw_title_text(self, img: Image.Image, title: str) -> Image.Image:
        """タイトルテキストを画像に描画（センターセーフ方式：1:1トリミング対応）
        
        Args:
            img: ベース画像
            title: タイトルテキスト
        
        Returns:
            Image.Image: テキスト描画後の画像
        """
        from budoux import load_default_japanese_parser
        draw = ImageDraw.Draw(img)
        
        # センターセーフ方式：画像の高さ(720)の90%を最大幅とする
        # これにより、1:1トリミング時も文字が中央に収まる
        max_text_width = int(self.THUMBNAIL_HEIGHT * 0.9)  # 720 * 0.9 = 648px
        max_text_height = int(self.THUMBNAIL_HEIGHT * 0.8)
        
        # フォントパスを取得
        font_path = self.font_paths[0] if self.font_paths else None
        if not font_path:
            font = ImageFont.load_default()
            lines = [title]
        else:
            # BudouXで自然な改行位置を取得
            parser = load_default_japanese_parser()
            chunks = parser.parse(title)
            lines = self._wrap_text_budoux(chunks, max_text_width, font_path)
            if not lines:
                lines = [title]
            
            # 初期フォントサイズを180（極大）に設定
            current_size = 180
            min_size = 40
            font = None
            
            # テキスト全体が画像内に収まるまでサイズを縮小
            while current_size >= min_size:
                try:
                    font = ImageFont.truetype(str(font_path), current_size)
                except OSError:
                    font = ImageFont.load_default()
                    break
                
                # 全行のバウンディングボックスを計算
                total_width = 0
                total_height = 0
                line_heights = []
                
                for line in lines:
                    bbox = draw.textbbox((0, 0), line, font=font)
                    line_width = bbox[2] - bbox[0]
                    line_height = bbox[3] - bbox[1]
                    total_width = max(total_width, line_width)
                    line_heights.append(line_height)
                
                # 行間はフォントサイズの20%
                line_spacing = int(current_size * 0.2)
                total_height = sum(line_heights) + line_spacing * (len(lines) - 1)
                
                # 幅と高さが両方とも収まればOK
                if total_width <= max_text_width and total_height <= max_text_height:
                    break
                
                # 収まらない場合はサイズを下げて再試行
                current_size -= 5
            
            # 最小サイズでも入らない場合は最小サイズを使用
            if current_size < min_size:
                font = ImageFont.truetype(str(font_path), min_size)
        
        # 最終的な行高と総高を再計算
        line_heights = []
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_heights.append(bbox[3] - bbox[1])
        
        # 行間はフォントサイズに応じて調整
        font_size = font.size if hasattr(font, 'size') else 60
        line_spacing = int(font_size * 0.2)
        total_height = sum(line_heights) + line_spacing * (len(lines) - 1)
        
        # 中央配置の開始Y座標
        y = (self.THUMBNAIL_HEIGHT - total_height) // 2
        
        # 各行を描画
        for line, line_height in zip(lines, line_heights):
            # テキストの幅を計算して中央揃え
            bbox = draw.textbbox((0, 0), line, font=font)
            text_width = bbox[2] - bbox[0]
            x = (self.THUMBNAIL_WIDTH - text_width) // 2
            
            # 黒フチ（ストローク）を描画
            self._draw_text_with_outline(
                draw, (x, y), line, font,
                fill_color="white",
                outline_color="black",
                outline_width=6
            )
            
            y += line_height + 20  # 次の行へ
        
        return img
    
    def _wrap_text_budoux(self, chunks: list[str], max_width: int, font_path: Path) -> list[str]:
        """文節区切りされたチャンクを指定幅で折り返す
        
        Args:
            chunks: BudouXで分割された文節リスト
            max_width: 最大幅（ピクセル）
            font_path: フォントパス
        
        Returns:
            list[str]: 折り返された行のリスト
        """
        # 仮のフォントサイズで計測（後で調整される）
        test_font = ImageFont.truetype(str(font_path), 100)
        draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
        
        lines = []
        current_line = ""
        
        for chunk in chunks:
            test_line = current_line + chunk
            bbox = draw.textbbox((0, 0), test_line, font=test_font)
            line_width = bbox[2] - bbox[0]
            
            if line_width <= max_width or not current_line:
                current_line = test_line
            else:
                lines.append(current_line)
                current_line = chunk
        
        if current_line:
            lines.append(current_line)
        
        return lines
    
    def _calculate_optimal_font_size(
        self,
        text: str,
        max_width: int,
        draw: ImageDraw.ImageDraw,
        max_font_size: int = 100,
        min_font_size: int = 40
    ) -> int:
        """テキストが指定幅に収まる最適なフォントサイズを計算
        
        Args:
            text: 計測するテキスト
            max_width: 最大幅（ピクセル）
            draw: ImageDrawオブジェクト
            max_font_size: 最大フォントサイズ
            min_font_size: 最小フォントサイズ
        
        Returns:
            int: 最適なフォントサイズ
        """
        current_size = max_font_size
        
        while current_size >= min_font_size:
            font = self._load_font(size=current_size)
            text_width = self._measure_text_width(text, font, draw)
            
            # 最大幅に収まればOK
            if text_width <= max_width:
                return current_size
            
            # サイズを減らして再チェック
            current_size -= 5
        
        # 最小サイズに達した
        return min_font_size
    
    def _measure_text_width(
        self,
        text: str,
        font: ImageFont.FreeTypeFont,
        draw: ImageDraw.ImageDraw
    ) -> int:
        """テキストの描画幅を計測
        
        Args:
            text: 計測するテキスト
            font: フォント
            draw: ImageDrawオブジェクト
        
        Returns:
            int: テキスト幅（ピクセル）
        """
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    
    def _get_fitted_font(self, draw, text, font_path, max_width, max_size=120, min_size=40):
        """指定された幅(max_width)に収まる最大のフォントサイズを計算して返す"""
        current_size = max_size
        
        while current_size >= min_size:
            try:
                font = ImageFont.truetype(str(font_path), current_size)
            except OSError:
                font = ImageFont.load_default()
                break
            
            # テキストの描画サイズを取得 (left, top, right, bottom)
            bbox = draw.textbbox((0, 0), text, font=font)
            text_width = bbox[2] - bbox[0]
            
            if text_width <= max_width:
                return font
            
            # 幅に収まらない場合はサイズを下げて再試行
            current_size -= 5
            
        # 最小サイズでも入らない場合は最小サイズを返す
        return ImageFont.truetype(str(font_path), min_size)
    
    def _load_font(self, size: int) -> ImageFont.FreeTypeFont:
        """フォントを読み込み
        
        Args:
            size: フォントサイズ
        
        Returns:
            ImageFont.FreeTypeFont: フォントオブジェクト
        """
        for font_path in self.font_paths:
            try:
                return ImageFont.truetype(str(font_path), size)
            except Exception:
                continue
        
        # フォールバック: デフォルトフォント
        console.print("[yellow]⚠ カスタムフォントの読み込みに失敗。デフォルトフォントを使用します。[/yellow]")
        return ImageFont.load_default()
    
    def _wrap_text(
        self,
        text: str,
        font: ImageFont.FreeTypeFont,
        max_width: int
    ) -> list[str]:
        """テキストを指定幅で改行
        
        Args:
            text: 元のテキスト
            font: フォント
            max_width: 最大幅（ピクセル）
        
        Returns:
            list[str]: 改行されたテキスト行のリスト
        """
        lines = []
        words = text.split()
        
        if not words:
            return [text]
        
        current_line = words[0]
        
        for word in words[1:]:
            # 仮の行を作成
            test_line = current_line + " " + word
            
            # 幅を計算
            bbox = ImageDraw.Draw(Image.new('RGB', (1, 1))).textbbox(
                (0, 0), test_line, font=font
            )
            width = bbox[2] - bbox[0]
            
            if width <= max_width:
                current_line = test_line
            else:
                lines.append(current_line)
                current_line = word
        
        lines.append(current_line)
        
        # 最大3行に制限
        if len(lines) > 3:
            lines = lines[:3]
            lines[-1] += "..."
        
        return lines
    
    def _draw_text_with_outline(
        self,
        draw: ImageDraw.ImageDraw,
        position: tuple[int, int],
        text: str,
        font: ImageFont.FreeTypeFont,
        fill_color: str = "white",
        outline_color: str = "black",
        outline_width: int = 3
    ) -> None:
        """アウトライン付きテキストを描画
        
        Args:
            draw: ImageDrawオブジェクト
            position: 描画位置 (x, y)
            text: テキスト
            font: フォント
            fill_color: 塗りつぶし色
            outline_color: アウトライン色
            outline_width: アウトライン幅
        """
        x, y = position
        
        # アウトラインを描画（8方向 + 斜め）
        for dx in range(-outline_width, outline_width + 1):
            for dy in range(-outline_width, outline_width + 1):
                if dx != 0 or dy != 0:
                    draw.text((x + dx, y + dy), text, font=font, fill=outline_color)
        
        # メインテキストを描画
        draw.text((x, y), text, font=font, fill=fill_color)
    
    def _draw_date_badge(self, img: Image.Image) -> Image.Image:
        """セーフエリア内の右上に日付バッジを描画（1:1トリミング対応）
        
        Args:
            img: ベース画像
        
        Returns:
            Image.Image: バッジ描画後の画像
        """
        draw = ImageDraw.Draw(img)
        
        # 現在の日付を取得
        badge_text = datetime.now().strftime("%Y.%m.%d制作")
        font_size = 45
        
        # フォント設定
        font = None
        for font_path in self.font_paths:
            try:
                font = ImageFont.truetype(str(font_path), font_size)
                break
            except Exception:
                continue
        
        if not font:
            font = ImageFont.load_default()
        
        # テキストサイズを計測
        bbox = draw.textbbox((0, 0), badge_text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        
        # バッジの位置とサイズ
        margin = 50
        padding_x = 25
        padding_y = 20
        badge_width = text_width + padding_x * 2
        badge_height = text_height + padding_y * 2
        
        # センターセーフ方式：セーフエリア(720x720)内の右上に配置
        # セーフエリアの右端 = (画像幅 / 2) + (セーフエリア幅 / 2)
        safe_zone_width = self.THUMBNAIL_HEIGHT  # 720px
        safe_zone_right = (self.THUMBNAIL_WIDTH // 2) + (safe_zone_width // 2)
        badge_x = safe_zone_right - badge_width - margin
        badge_y = margin
        
        # 角丸長方形の背景を描画
        badge_rect = [
            badge_x,
            badge_y,
            badge_x + badge_width,
            badge_y + badge_height
        ]
        
        # 背景色（赤）
        draw.rounded_rectangle(
            badge_rect,
            radius=10,
            fill="#CC0000",
            outline=None
        )
        
        # テキストを中央に配置
        text_x = badge_x + padding_x
        text_y = badge_y + padding_y
        
        # 白文字で描画
        draw.text(
            (text_x, text_y),
            badge_text,
            font=font,
            fill="#FFFFFF"
        )
        
        return img
    
    def regenerate_with_new_title(
        self,
        theme: str,
        script_summary: str,
        output_dir: str,
        background_path: str,
        base_title: str,
        generation_count: int = 0
    ) -> Tuple[str, str, str]:
        """新しいタイトルでサムネイルを再生成
        
        Args:
            theme: 元のテーマ
            script_summary: 台本要約
            output_dir: 出力先ディレクトリ
            background_path: 背景画像パス
            base_title: 元の動画タイトル
            generation_count: 再生成回数
            
        Returns:
            Tuple[str, str, str]: (thumbnail_path, video_title, thumbnail_title)
        """
        import asyncio
        import concurrent.futures

        from core.interfaces.llm_port import LLMRequest
        from core.models.config import load_config
        from core.prompt_manager import PromptManager
        from services.script_generation.adapters.factory import LLMAdapterFactory

        try:
            console.print(f"[cyan]🔄 新しいサムネイルタイトルを生成中...[/cyan]")

            # 1. Ollama で新しいタイトル生成（軽量モデル）
            # Step 4 v2 (2026-05-10): Gemini 経路は削除済み。orchestrator.curator_model を再利用。
            app_config = load_config()
            prompt_manager = PromptManager()

            regeneration_prompt = prompt_manager.get_prompt("thumbnail_regeneration", "default")
            formatted_prompt = regeneration_prompt.format(
                theme=theme,
                script_summary=script_summary
            )

            # 軽量モデルで API 呼び出し（LLMAdapterFactory 経由 / Ollama）
            flash_model = app_config.yaml.script_generator.orchestrator.curator_model
            llm_port = LLMAdapterFactory.create(
                app_config,
                "ollama",
                model_override=flash_model,
            )
            llm_request = LLMRequest(
                system_prompt="",
                user_prompt=formatted_prompt,
                model=flash_model,
                max_tokens=16384,
                temperature=0.85,
                response_format="json",
            )
            # regenerate_with_new_title は同期 API として呼ばれる（Gradio の sync /
            # async 双方のハンドラから呼び出される）。呼び出し元のループ状態に依存
            # しない確実なブリッジとして、別スレッドで asyncio.run する。
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
                llm_response = _ex.submit(asyncio.run, llm_port.generate(llm_request)).result()
            response_text = llm_response.content
            
            # レスポンスをパース
            metadata = json.loads(response_text.strip())
            new_thumbnail_title = metadata.get("thumbnail_title", "新着")
            new_video_title = metadata.get("title", base_title)
            
            console.print(f"[green]✓ 新しいタイトル生成完了[/green]")
            console.print(f"  サムネイル文字: {new_thumbnail_title}")
            console.print(f"  動画タイトル: {new_video_title[:30]}...")
            
            # 2. タイムスタンプ付きでサムネイル生成
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            version_suffix = f"_v{generation_count + 1}" if generation_count > 0 else ""
            thumbnail_filename = f"thumbnail_regenerated{version_suffix}_{timestamp}.png"
            thumbnail_path = Path(output_dir) / thumbnail_filename
            
            console.print(f"[cyan]🖼️ サムネイル画像を生成中...[/cyan]")
            
            # 既存のgenerateメソッドを呼び出し
            self.generate(
                title=new_video_title,
                background_path=Path(background_path),
                output_path=thumbnail_path,
                thumbnail_title=new_thumbnail_title
            )
            
            console.print(f"[green]✓ サムネイル再生成完了[/green] {thumbnail_path.name}")
            
            return (
                str(thumbnail_path),
                new_video_title,
                new_thumbnail_title
            )
            
        except Exception as e:
            console.print(f"[red]❌ サムネイル再生成エラー: {str(e)}[/red]")
            raise
