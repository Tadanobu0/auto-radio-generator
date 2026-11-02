"""YouTube概要欄用テキストサニタイズユーティリティ"""
import re
import unicodedata
from urllib.parse import urlparse

# YouTubeで問題になりやすい文字の制御
# NOTE: \x09(Tab), \x0A(LF), \x0D(CR) は構造維持のため除外する
CONTROL_CHARS = re.compile(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]')

# YouTubeチャプター認識を妨げる可能性のある文字
# 許可範囲:
#   - 英数字/_、空白、-、:、()、.
#   - CJK記号類 U+3000-U+303F（、。「」『』〜・など）
#   - ひらがな U+3040-U+309F
#   - カタカナ U+30A0-U+30FF（ー・など含む）
#   - CJK統合漢字 U+4E00-U+9FFF
#   - 全角感嘆符！(U+FF01) / 全角疑問符？(U+FF1F)
#   - 三点リーダ … (U+2026)
# これらはYouTubeチャプターで実用上問題なく認識されるため許容する
CHAPTER_PROBLEMATIC = re.compile(
    r'[^\w\s\-:().'
    r'\u3000-\u303F'
    r'\u3040-\u309F'
    r'\u30A0-\u30FF'
    r'\u4E00-\u9FFF'
    r'\uFF01\uFF1F'
    r'\u2026'
    r']'
)

# YouTube はタイトル・概要欄の半角 < > を受け付けない (invalidTitle / invalidDescription)。
# 2026-11-02: 参考文献の題に PDF の内部名 <4D6963…> が入り、概要欄が拒否された。
# 既存の制御文字・URL と同じく「落とす」で揃える (radio_director 側も同じ)。
ANGLE_BRACKETS = re.compile(r'[<>]')

# PDF の内部名など、題が <16 進> だけのもの (例: <4D6963726F736F6674>)
HEX_ONLY_TITLE = re.compile(r'^\s*<\s*[0-9A-Fa-f]+\s*>\s*$')


def strip_angle_brackets(text: str) -> str:
    """半角の < > を取り除く (YouTube のタイトル・概要欄で拒否されるため)。"""
    if not text:
        return text or ""
    return ANGLE_BRACKETS.sub('', text)


def is_hex_only_title(title: str) -> bool:
    """題が <16 進> だけ (PDF の内部名) なら True。"""
    return bool(title) and bool(HEX_ONLY_TITLE.match(title))


# 許可する絵文字（YouTubeで安全に表示されるもの）
ALLOWED_EMOJIS = {'📄', '🔗', '🎵', '🎬', '📝', '🎯', '🎪', '🎭'}

# URL検証用正規表現（http/httpsのみ許可）
URL_PATTERN = re.compile(
    r'^https?://'  # http:// or https://のみ
    r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # domain...
    r'localhost|'  # localhost...
    r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
    r'(?::\d+)?'  # optional port
    r'(?:/?|[/?]\S+)$', re.IGNORECASE)


def sanitize_for_youtube(text: str, max_length: int = 5000) -> str:
    """YouTube用にテキストをサニタイズ
    
    Args:
        text: サニタイズ対象のテキスト
        max_length: 最大文字数（デフォルト: 5000）
        
    Returns:
        サニタイズされたテキスト
    """
    if not text:
        return ""
    
    # Unicode正規化（NFC形式）
    text = unicodedata.normalize('NFC', text)
    
    # 制御文字を削除
    text = CONTROL_CHARS.sub('', text)

    # 半角 < > を削除 (YouTube が invalidDescription / invalidTitle で拒否する)
    text = strip_angle_brackets(text)

    # 長さ制限
    if len(text) > max_length:
        # 参考文献セクションを優先的に切り詰め
        lines = text.split('\n')
        ref_section_start = -1
        
        for i, line in enumerate(lines):
            if '【参考文献】' in line:
                ref_section_start = i
                break
        
        if ref_section_start >= 0:
            # 参考文献セクションを短くする
            before_refs = lines[:ref_section_start]
            ref_lines = lines[ref_section_start:]
            
            # 参考文献を1件ずつ削除して長さを調整
            while len('\n'.join(before_refs + ref_lines)) > max_length and len(ref_lines) > 3:
                # 参考文献アイテムを削除（3行: タイトル、URL、空行）
                if len(ref_lines) >= 4:
                    ref_lines = ref_lines[:-4]  # 最後の参考文献を削除
                else:
                    break
            
            text = '\n'.join(before_refs + ref_lines)
        
        # それでも長い場合は末尾を切り詰め
        if len(text) > max_length:
            text = text[:max_length-3] + '...'
    
    return text


def sanitize_title(title: str) -> str:
    """WebページタイトルをYouTube表示用にサニタイズ
    
    Args:
        title: スクレイピングしたタイトル
        
    Returns:
        サニタイズされたタイトル
    """
    if not title:
        return ""
    
    # Unicode正規化
    title = unicodedata.normalize('NFC', title)
    
    # 制御文字を削除
    title = CONTROL_CHARS.sub('', title)
    
    # 連続する空白を単一化
    title = re.sub(r'\s+', ' ', title)
    
    # 前後の空白を削除
    title = title.strip()
    
    # 長すぎる場合は省略
    if len(title) > 100:
        title = title[:97] + '...'
    
    return title


def validate_url(url: str) -> bool:
    """URLが有効な形式か検証
    
    Args:
        url: 検証するURL文字列
        
    Returns:
        有効な場合はTrue
    """
    if not url:
        return False
    
    try:
        result = urlparse(url)
        # http/httpsのみ許可
        if result.scheme not in ('http', 'https'):
            return False
        return all([result.scheme, result.netloc])
    except Exception:
        return False


def normalize_url(url: str) -> str:
    """URLを正規化（https統一、末尾スラッシュ削除）
    
    Args:
        url: 正規化するURL
        
    Returns:
        正規化されたURL
    """
    if not url:
        return url
    
    # httpをhttpsに統一
    if url.startswith('http://'):
        url = 'https://' + url[7:]
    
    # 末尾のスラッシュを削除
    if url.endswith('/') and url != 'https://':
        url = url[:-1]
    
    return url


def validate_chapter_format(chapter_lines: list[str]) -> bool:
    """チャプター形式がYouTube認識可能か検証
    
    Args:
        chapter_lines: チャプター行のリスト
        
    Returns:
        有効な場合はTrue
    """
    for line in chapter_lines:
        if not line.strip():
            continue
            
        # MM:SS 形式を検証
        time_match = re.match(r'^(\d{1,2}):(\d{2})\s+(.+)$', line.strip())
        if not time_match:
            return False
        
        minutes, seconds, title = time_match.groups()
        
        # 時間の妥当性チェック
        try:
            min_val = int(minutes)
            sec_val = int(seconds)
            if sec_val >= 60 or min_val < 0:
                return False
        except ValueError:
            return False
        
        # タイトルに問題文字が含まれていないか
        if CHAPTER_PROBLEMATIC.search(title):
            return False
    
    return True


def filter_emojis(text: str) -> str:
    """絵文字をフィルタリング（許可リストのみ保持）
    
    Args:
        text: フィルタリングするテキスト
        
    Returns:
        フィルタリングされたテキスト
    """
    if not text:
        return ""
    
    # 許可された絵文字以外を削除
    filtered_chars = []
    for char in text:
        # 許可された絵文字か、絵文字でない文字は保持
        if char in ALLOWED_EMOJIS or not unicodedata.category(char).startswith('So'):
            filtered_chars.append(char)
    
    return ''.join(filtered_chars)
