"""Export helpers — write search results to files."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def export_json(data: dict[str, Any], output: str | None) -> str | None:
    """Write JSON to file or return as string."""
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if output:
        Path(output).write_text(text, encoding="utf-8")
        return output
    return None


def export_markdown(text: str, output: str | None) -> str | None:
    """Write markdown to file or return as string."""
    if output:
        Path(output).write_text(text, encoding="utf-8")
        return output
    return None
