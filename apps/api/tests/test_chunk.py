"""Splitting a document's text into overlapping, retrieval-sized chunks."""

from app.knowledge.chunk import chunk_text


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_short_text_is_a_single_chunk() -> None:
    assert chunk_text("Just one short paragraph.", target=1500) == ["Just one short paragraph."]


def test_paragraphs_are_packed_up_to_the_target_size() -> None:
    """Several short paragraphs land in one chunk until adding the next would cross `target`."""
    paragraphs = ["A" * 40, "B" * 40, "C" * 40]
    text = "\n\n".join(paragraphs)

    chunks = chunk_text(text, target=90, overlap=0)

    # "A"*40 + "\n\n" + "B"*40 = 82 chars (<=90); adding "C"*40 would push past 90.
    assert chunks == ["A" * 40 + "\n\n" + "B" * 40, "C" * 40]


def test_a_paragraph_longer_than_target_is_split_at_whitespace() -> None:
    """A single wall-of-text paragraph with no blank lines still gets cut, at word boundaries,
    never mid-word."""
    long_paragraph = " ".join(["word"] * 100)  # 100 * 5 - 1 = 499 chars

    chunks = chunk_text(long_paragraph, target=50, overlap=0)

    assert len(chunks) > 1
    assert all(len(c) <= 50 for c in chunks)
    assert all(not c.startswith(" ") and not c.endswith(" ") for c in chunks)
    # No word was ever cut in half — every chunk is made of whole "word" tokens.
    assert all(set(c.split()) <= {"word"} for c in chunks)


def test_each_chunk_after_the_first_is_prefixed_with_the_previous_overlap() -> None:
    paragraphs = ["A" * 40, "B" * 40, "C" * 40]
    text = "\n\n".join(paragraphs)

    chunks = chunk_text(text, target=45, overlap=10)

    assert len(chunks) == 3
    assert chunks[0] == "A" * 40
    assert chunks[1] == ("A" * 40)[-10:] + "\n\n" + "B" * 40
    assert chunks[2] == ("B" * 40)[-10:] + "\n\n" + "C" * 40


def test_zero_overlap_returns_the_packed_chunks_unmodified() -> None:
    # Each paragraph (17 and 18 chars) fits target=20 on its own, but not combined (37 chars) —
    # so they land in separate chunks, neither one split internally.
    text = "First paragraph.\n\nSecond paragraph."
    chunks = chunk_text(text, target=20, overlap=0)
    assert chunks == ["First paragraph.", "Second paragraph."]
