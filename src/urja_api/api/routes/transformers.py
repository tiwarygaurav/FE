from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Path, Query
from pydantic import Field

from ...domain.models import MeterStatus, MeterSummary, NetworkPath, Page, Transformer
from ...store import MeterFilter
from ..deps import IndexedServices
from ..errors import ApiProblem, problems
from ..views import meter_summary

router = APIRouter(prefix="/v1/transformers", tags=["transformers"])


class TransformerDetail(Transformer):
    meters: list[MeterSummary] = Field(description="Meters connected to this transformer.")


def _transformer(row, status_counts: dict[str, int]) -> Transformer:
    return Transformer(
        code=row["code"],
        name=row["name"],
        feeder_code=row["feeder_code"],
        capacity_kva=row["capacity_kva"],
        network=NetworkPath.model_validate_json(row["network_json"]),
        meter_count=row["meter_count"],
        meters_by_status={MeterStatus(k): v for k, v in sorted(status_counts.items())},
        name_variants=json.loads(row["name_variants_json"]),
    )


@router.get(
    "", response_model=Page[Transformer], responses=problems(401, 422, 503), summary="List distribution transformers"
)
def list_transformers(
    services: IndexedServices,
    feeder: Annotated[str | None, Query(description="Only transformers on this feeder, e.g. `F-001`.")] = None,
    limit: Annotated[int, Query(ge=1, le=500, description="Page size.")] = 100,
    offset: Annotated[int, Query(ge=0, description="Items to skip.")] = 0,
) -> Page[Transformer]:
    status = services.store.meter_status_by_transformer()
    rows = [r for r in services.store.list_transformers() if not feeder or r["feeder_code"] == feeder.strip().upper()]
    items = [_transformer(r, status.get(r["code"], {})) for r in rows[offset : offset + limit]]
    return Page[Transformer](items=items, total=len(rows), limit=limit, offset=offset)


@router.get(
    "/{code}",
    response_model=TransformerDetail,
    responses=problems(401, 404, 422, 503),
    summary="Get one transformer with its meters",
)
def get_transformer(
    code: Annotated[str, Path(description="Transformer code (any case).", examples=["DT-001"])],
    services: IndexedServices,
) -> TransformerDetail:
    code = code.strip().upper()
    row = services.store.get_transformer(code)
    if row is None:
        raise ApiProblem(404, "transformer_not_found", f"No transformer with code {code!r}.")
    status = services.store.meter_status_by_transformer().get(code, {})
    meters, _ = services.store.search_meters(MeterFilter(network={"transformer": code}), limit=10_000, offset=0)
    return TransformerDetail(**_transformer(row, status).model_dump(), meters=[meter_summary(m) for m, _ in meters])
