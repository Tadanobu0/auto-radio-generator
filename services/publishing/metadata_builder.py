"""YouTube動画メタデータ構築ユーティリティ"""
from __future__ import annotations

import logging
import re
from typing import List
from urllib.parse import urlparse

from services.publishing.text_sanitizer import sanitize_for_youtube, validate_chapter_format
from core.models.research import ResearchSource

logger = logging.getLogger(__name__)

ReferenceEntry = str | ResearchSource

# 免責文 (全動画一律)。説明欄末尾 (使用音声クレジット = footer の直前) に挿入する。
MEDICAL_DISCLAIMER = "※本動画は一般的な情報提供を目的としており、医学的助言ではありません。"

# 概要欄に載せてはいけない URL (http:// / https:// で始まる文字列)。
# 2026-10-24: 出典リンク先のドメイン分類を理由に動画が削除されたため、
# 概要欄には URL を一切載せない (出典は「タイトル(ドメイン名)」で示す)。
URL_IN_TEXT_PATTERN = re.compile(r"https?://\S*", re.IGNORECASE)


def reference_domain(url: str) -> str:
    """URL からドメイン名 (先頭の www. を除く) を取り出す。取れなければ空文字。"""
    try:
        host = urlparse((url or "").strip()).hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def format_reference_label(ref: ReferenceEntry, idx: int) -> str:
    """出典 1 件を URL を含まない「タイトル(ドメイン名)」表記にする。

    title が無ければ「参考文献N」、published_date があればドメインの後ろに付ける:
        環状オリゴ糖の脂質吸収抑制効果(example.com、2026-06-28)
    """
    if isinstance(ref, ResearchSource):
        title = (ref.title or "").strip() or f"参考文献{idx}"
        url = (ref.url or "").strip()
        published = (getattr(ref, "published_date", None) or "").strip()
    else:
        title = f"参考文献{idx}"
        url = (ref or "").strip()
        published = ""
    meta = [part for part in (reference_domain(url), published) if part]
    label = f"{title}({'、'.join(meta)})" if meta else title
    # タイトル自体に URL が入っているソースもあるので、ここでも落とす
    return strip_urls(label)[0] or f"参考文献{idx}"


def strip_urls(text: str) -> tuple[str, List[str]]:
    """テキストから URL を取り除き、(取り除いた後のテキスト, 取り除いた URL) を返す。

    URL を消した結果、絵文字や記号だけになった行は行ごと落とす。
    """
    removed: List[str] = []
    out_lines: List[str] = []
    for line in (text or "").split("\n"):
        found = URL_IN_TEXT_PATTERN.findall(line)
        if not found:
            out_lines.append(line)
            continue
        removed.extend(found)
        cleaned = URL_IN_TEXT_PATTERN.sub("", line).rstrip()
        if not re.search(r"[\w぀-ヿ一-鿿]", cleaned):
            continue
        out_lines.append(cleaned)
    return "\n".join(out_lines), removed


def _normalize_non_empty(items: List[str]) -> List[str]:
    """Trim values, drop empties, and preserve order while deduplicating."""
    seen = set()
    normalized: List[str] = []
    for item in items:
        value = (item or "").strip()
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _append_section_heading(lines: List[str], heading: str, below_blank_lines: int = 0) -> None:
    """見出し前は2行、見出し後は0〜1行の空行で見出しを追加する。"""
    while lines and lines[-1] == "":
        lines.pop()
    lines.extend(["", "", heading])

    if below_blank_lines not in (0, 1):
        raise ValueError("below_blank_lines must be 0 or 1")

    lines.extend([""] * below_blank_lines)


def _normalize_references(items: List[ReferenceEntry]) -> List[ReferenceEntry]:
    """Trim values, drop empties, and preserve order while deduplicating by URL/string."""
    seen = set()
    normalized: List[ReferenceEntry] = []
    for item in items:
        if isinstance(item, ResearchSource):
            url = (item.url or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            normalized.append(item)
            continue

        value = (item or "").strip()
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def normalize_hashtags(
    dynamic_tags: List[str],
    fixed_tags: List[str],
    max_total: int = 60,
) -> List[str]:
    """テーマ由来タグ先頭 + 固定タグ末尾の # 付きハッシュタグ列を構築する。

    - # 未付与のタグには # を付与する (既に付いていれば二重付与しない)
    - 順序: dynamic (テーマ由来) → fixed (固定)。YouTube はタイトル上に
      先頭 3 つを表示するため、動画ごとに変わるテーマ由来タグを先頭に置く
    - YouTube 仕様 (ハッシュタグ 60 個超で全タグ無効) に合わせ、
      合計が max_total を超える分は末尾から落とす
    """
    def _hashify(tag: str) -> str:
        value = (tag or "").strip()
        if not value:
            return ""
        return value if value.startswith("#") else f"#{value}"

    merged = _normalize_non_empty([_hashify(t) for t in [*dynamic_tags, *fixed_tags]])
    return merged[:max_total]


def format_reference_text_lines(references: List[ReferenceEntry]) -> List[str]:
    """metadata.txt 向けの参考文献行を構築する。

    metadata.txt の参考文献は「概要欄用テキスト」として手でコピーされるため、
    概要欄と同じく URL を載せず 1 文献 1 行の「タイトル(ドメイン名)」にする:
        参考文献1: 環状オリゴ糖の脂質吸収抑制効果(example.com、2026-06-28)
    """
    lines: List[str] = []
    for idx, ref in enumerate(_normalize_references(references), start=1):
        label = format_reference_label(ref, idx)
        # title 無しは label 自体が「参考文献N(...)」なので番号を重ねない
        lines.append(label if label.startswith(f"参考文献{idx}") else f"参考文献{idx}: {label}")
    return lines


def build_video_description(
    script_description: str,
    chapters: List[str],
    references: List[ReferenceEntry],
    dynamic_tags: List[str],
    fixed_tags: List[str],
    footer_text: str,
    llm_model_info: str = "",
) -> str:
    """Build a rich YouTube description with a fixed, maintainable structure.

    構成順序:
    1. 【動画の概要】(script_description)
    2. <空行>
    3. 【目次】(chapters - 00:00 形式)
    4. <空行>
    5. 【参考文献】(references - 1 件 1 行の「📄 タイトル(ドメイン名)」。URL は載せない)
    6. <空行>
    7. 【タグ】(dynamic_tags + fixed_tags)
    8. <空行>
    9. llm_model_info (使用モデル情報)
    10. <空行>
    11. footer_text
    """
    description_text = (script_description or "").strip() or "（概要未設定）"
    chapter_lines = _normalize_non_empty(chapters)
    reference_lines = _normalize_references(references)

    # 2026-07-17: # 付与 + テーマ由来先頭/固定末尾 + 60 個上限の正規化に変更
    merged_tags = normalize_hashtags(dynamic_tags, fixed_tags)
    footer = (footer_text or "").strip() or "（フッター未設定）"

    lines: List[str] = ["【動画の概要】", description_text]

    _append_section_heading(lines, "【目次】", below_blank_lines=0)
    if chapter_lines:
        lines.extend(chapter_lines)
    else:
        lines.append("- チャプター情報なし")
    _append_section_heading(lines, "【参考文献】", below_blank_lines=0)
    if reference_lines:
        # 2026-10-24: URL 行 (🔗) を廃止。リンク先ドメインの分類で動画が削除される
        # ため、出典は「タイトル(ドメイン名)」の 1 行だけにする
        for idx, ref in enumerate(reference_lines, start=1):
            lines.append(f"📄 {format_reference_label(ref, idx)}")
    else:
        lines.append("- 参考文献なし")
    _append_section_heading(lines, "【タグ】", below_blank_lines=0)
    if merged_tags:
        lines.append(" ".join(merged_tags))
    else:
        lines.append("#ラジオ")

    # 使用モデル情報を追加（存在する場合）
    if llm_model_info:
        lines.append("")
        lines.append("")
        lines.append(llm_model_info)

    # 免責文 (全動画一律)。footer = 使用音声クレジットの直前に置く
    lines.append("")
    lines.append(MEDICAL_DISCLAIMER)
    lines.append("")
    lines.append(footer)

    # 最終的なサニタイズと検証
    final_text = "\n".join(lines).strip()
    final_text = sanitize_for_youtube(final_text, max_length=5000)
    # 概要文 (radio_director 由来)・フッター等に紛れた URL もここで落とす
    final_text, removed_urls = strip_urls(final_text)
    if removed_urls:
        logger.warning("概要欄から URL を %d 件取り除きました: %s", len(removed_urls), removed_urls)
    final_text = final_text.strip()
    
    # チャプター形式の検証
    if chapter_lines and not validate_chapter_format(chapter_lines):
        logger.warning("チャプター形式がYouTubeに認識されない可能性があります")

    return final_text
