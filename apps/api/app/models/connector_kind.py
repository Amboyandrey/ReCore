"""What a connector indexes — a website crawl, or a set of uploaded files."""

import enum


class ConnectorKind(enum.StrEnum):
    WEBSITE = "website"
    FILE = "file"
