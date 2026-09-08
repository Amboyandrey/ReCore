"""How far an attachment's text extraction got — read by the chat pipeline before it trusts it."""

import enum


class ExtractStatus(enum.StrEnum):
    """Extraction is synchronous today (see services/attachments.py) but still has a real state
    machine, since not every file type yields text."""

    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    # An image (or other format the model reads directly): no text was extracted because none
    # was needed, not because extraction failed or was skipped. DONE with empty text would be
    # indistinguishable from a failure; UNSUPPORTED would simply be a lie.
    PASSTHROUGH = "passthrough"
