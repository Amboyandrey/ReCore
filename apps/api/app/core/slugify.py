"""Turns a display name into a URL-safe slug — used for workspace URLs like /w/acme-inc."""

import re


def slugify(text: str) -> str:
    """Lowercase, hyphenate, and strip anything that isn't alphanumeric."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "workspace"
