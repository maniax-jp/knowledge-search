"""Text chunking for the knowledge index. Stdlib only."""

from __future__ import annotations


def chunk_text(text: str, size: int = 1600, min_tail: int = 200) -> list[str]:
    """Split text into paragraph-aware chunks.

    No overlap: GET reassembles chunks by joining them. A term that straddles
    a boundary can miss; 1600 characters keeps that rare for Japanese prose.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(para) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_window(para, size))
            continue
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) <= size:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
        buf = para
    if buf:
        chunks.append(buf)

    if min_tail and len(chunks) >= 2 and len(chunks[-1]) < min_tail:
        tail = chunks.pop()
        chunks[-1] = f"{chunks[-1]}\n\n{tail}"
    return chunks


def _window(text: str, size: int) -> list[str]:
    parts: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            cut = text.rfind("\n", start + size // 2, end)
            if cut == -1:
                cut = text.rfind("。", start + size // 2, end)
            if cut != -1 and cut > start:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            parts.append(piece)
        if end <= start:
            end = start + size
        start = end
    return parts
