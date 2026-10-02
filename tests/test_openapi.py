"""The committed OpenAPI document is the published contract: it must match the app."""

from __future__ import annotations

import json
import re
from pathlib import Path

from urja_api.api.app import create_app
from urja_api.api.errors import ERROR_CODES

ROOT = Path(__file__).resolve().parent.parent
OPENAPI_PATH = ROOT / "openapi.json"


def operations(schema: dict) -> list[tuple[str, str, dict]]:
    return [(path, method, op) for path, ops in schema["paths"].items() for method, op in ops.items()]


def test_committed_openapi_matches_the_app():
    committed = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    generated = json.loads(json.dumps(create_app().openapi()))
    assert committed == generated, "openapi.json is out of date: run `uv run python scripts/export_openapi.py`"


def test_error_responses_are_problem_json():
    schema = create_app().openapi()
    for path, method, op in operations(schema):
        for status, response in op["responses"].items():
            if status[0] in "45":
                assert list(response["content"]) == ["application/problem+json"], (method, path, status)
                assert response["content"]["application/problem+json"]["schema"] == {
                    "$ref": "#/components/schemas/Problem"
                }
            if status == "503":
                assert "Retry-After" in response["headers"], (method, path)
    schemas = schema["components"]["schemas"]
    assert "HTTPValidationError" not in schemas  # every 422 is a Problem
    assert set(schemas["FieldError"]["properties"]) == {"location", "message", "type"}  # and its errors are typed


def test_the_api_key_is_optional_and_operation_ids_are_the_handler_names():
    schema = create_app().openapi()
    ids = [op["operationId"] for _, _, op in operations(schema)]
    assert len(set(ids)) == len(ids)
    assert {"list_meters", "get_readings", "get_consumption", "consumption_insight"} <= set(ids)
    for path, _, op in operations(schema):
        if path.startswith("/v1/"):
            assert op["security"] == [{}, {"APIKeyHeader": []}], path
    healthz = schema["paths"]["/healthz"]["get"]
    assert "security" not in healthz and list(healthz["responses"]) == ["200"]


def test_every_error_code_in_the_source_is_documented():
    source = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src" / "urja_api").rglob("*.py"))
    used = set(re.findall(r'ApiProblem\(\s*\d{3},\s*"([a-z_]+)"', source))
    used |= set(re.findall(r'\.get\(exc\.status_code, "([a-z_]+)"\)', source))  # the HTTP fallback
    assert used, "the pattern no longer finds any codes"
    assert used <= set(ERROR_CODES), used - set(ERROR_CODES)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert [code for code in ERROR_CODES if f"`{code}`" not in readme] == [], "README's error table is incomplete"
