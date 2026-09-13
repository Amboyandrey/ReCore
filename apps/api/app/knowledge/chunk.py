"""Splitting one document's extracted text into overlapping, retrieval-sized pieces.

Paragraph-aware rather than a fixed-width slice: packing whole paragraphs up to a target size
keeps each chunk a coherent unit of meaning (a fixed-width cut would routinely sever a sentence
mid-thought), while the overlap between consecutive chunks means a fact stated right at a chunk
boundary is still findable from whichever side of the cut the nearest-neighbor search lands on.
"""

import re

DEFAULT_TARGET_CHARS = 1500
DEFAULT_OVERLAP_CHARS = 200
# A document short enough to fit under this stays a single, unsplit chunk rather than being
# packed into several ~target-sized pieces. Without this, a short document (a one-page letter,
# quitclaim, or form) still gets split into 2-3 chunks, and retrieval keeps only the single
# nearest chunk per document — so a near-tie in embedding similarity between two chunks of the
# *same* short document can silently exclude the one that actually answers the query in favor of
# a boilerplate paragraph a few hundredths of a cosine-distance point "closer". Observed live: a
# 3654-char one-page quitclaim split into 3 chunks, where the chunk stating the settlement amount
# (dist 0.4900) narrowly lost to a generic release-of-liability chunk (dist 0.4706). Independent
# of `target` (not a multiple of it) — it answers "is this whole document short", not "would this
# take few packed chunks" — so tests exercising packing with a tiny `target` pass `single_chunk_max=0`
# to disable this shortcut rather than tripping over it by coincidence.
DEFAULT_SINGLE_CHUNK_MAX_CHARS = 4 * DEFAULT_TARGET_CHARS

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


def _split_oversize_paragraph(paragraph: str, *, target: int) -> list[str]:
    """A paragraph longer than `target` on its own (a wall of text with no blank lines) is cut at
    whitespace boundaries instead — never mid-word, but without waiting for a paragraph break
    that may never come."""
    words = paragraph.split()
    if not words:
        return []
    pieces: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        added = len(word) + (1 if current else 0)
        if current and current_len + added > target:
            pieces.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += added
    if current:
        pieces.append(" ".join(current))
    return pieces


def chunk_text(
    text: str,
    *,
    target: int = DEFAULT_TARGET_CHARS,
    overlap: int = DEFAULT_OVERLAP_CHARS,
    single_chunk_max: int = DEFAULT_SINGLE_CHUNK_MAX_CHARS,
) -> list[str]:
    """Pack `text`'s paragraphs into chunks of roughly `target` characters, each chunk after the
    first prefixed with the previous chunk's last `overlap` characters. Empty or whitespace-only
    text yields an empty list — nothing to index, not an error. A document short enough to fit
    under `single_chunk_max` is returned as a single chunk instead, however many paragraphs it
    has — see DEFAULT_SINGLE_CHUNK_MAX_CHARS for why.
    """
    paragraphs: list[str] = []
    for raw in _PARAGRAPH_SPLIT.split(text):
        paragraph = raw.strip()
        if not paragraph:
            continue
        paragraphs.append(paragraph)
    if not paragraphs:
        return []

    whole = "\n\n".join(paragraphs)
    if len(whole) <= single_chunk_max:
        return [whole]

    sized_paragraphs: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) > target:
            sized_paragraphs.extend(_split_oversize_paragraph(paragraph, target=target))
        else:
            sized_paragraphs.append(paragraph)
    paragraphs = sized_paragraphs

    packed: list[str] = []
    current_parts: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        added = len(paragraph) + (2 if current_parts else 0)  # "\n\n" joiner
        if current_parts and current_len + added > target:
            packed.append("\n\n".join(current_parts))
            current_parts = [paragraph]
            current_len = len(paragraph)
        else:
            current_parts.append(paragraph)
            current_len += added
    if current_parts:
        packed.append("\n\n".join(current_parts))

    if overlap <= 0 or len(packed) < 2:
        return packed
    # Deliberately not strict=True: packed[1:] is one element shorter than packed by
    # construction (every chunk but the last is paired with the one after it), not a bug to
    # guard against.
    chunks = [packed[0]]
    for previous, chunk in zip(packed, packed[1:], strict=False):
        chunks.append(previous[-overlap:] + "\n\n" + chunk)
    return chunks
