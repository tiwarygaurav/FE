"""Write the API's OpenAPI document to ``openapi.json`` at the repository root.

    uv run python scripts/export_openapi.py

`tests/test_openapi.py` fails when the committed file no longer matches the app.
"""

from __future__ import annotations

import json
from pathlib import Path

from urja_api.api.app import create_app

OPENAPI_PATH = Path(__file__).resolve().parent.parent / "openapi.json"


def main() -> None:
    OPENAPI_PATH.write_text(json.dumps(create_app().openapi(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OPENAPI_PATH}")


if __name__ == "__main__":
    main()
