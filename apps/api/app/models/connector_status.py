"""Where a connector is in its indexing lifecycle."""

import enum


class ConnectorStatus(enum.StrEnum):
    PENDING = "pending"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"
