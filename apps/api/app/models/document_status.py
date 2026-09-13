"""Whether one connector document's text was extracted and chunked yet."""

import enum


class DocumentStatus(enum.StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
