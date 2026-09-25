"""UmbraNet version labels and ordering: 26.0.1a < 26.0.1b < 26.0.1r.

Keep the public label separate from the PEP 440 comparison value. In particular,
packaging interprets a bare 'r' as a post-release alias, not our stable-release
marker. Older dev/rc/plain version tags remain readable during migration.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

_SHORT_VERSION = re.compile(r"v?([0-9]+)\.([0-9]+)\.([0-9]+)([abr])", re.IGNORECASE)


@dataclass(frozen=True)
class AppVersion:
    value: Version
    display: str


def parse_app_version(text: str) -> AppVersion:
    """Accept an app label/GitHub tag or a legacy PEP 440 version.

    a/b are prereleases (internally a0/b0); r is exactly the plain stable version,
    not .post0. The numeric release components always take priority over stage.
    The normalized comparison value must never replace the short display label.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 100:
        raise InvalidVersion("Expected a version label of at most 100 characters")
    text = text.strip()
    match = _SHORT_VERSION.fullmatch(text)
    if match:
        major, minor, patch, stage = match.groups()
        base = ".".join(str(int(part)) for part in (major, minor, patch))
        stage = stage.lower()
        suffix = {"a": "a0", "b": "b0", "r": ""}[stage]
        return AppVersion(Version(base + suffix), base + stage)
    legacy = Version(text)
    return AppVersion(legacy, str(legacy))
