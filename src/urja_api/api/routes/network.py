from __future__ import annotations

import json
from collections import Counter
from typing import Annotated

from fastapi import APIRouter, Path, Query

from ...domain.models import (
    DataIssue,
    DataQualityReport,
    MeterIssues,
    MeterStatus,
    NetworkEdge,
    NetworkLevel,
    NetworkNeighbour,
    NetworkNode,
    NetworkNodeSummary,
    NetworkOverview,
    NetworkTreeNode,
    ReadingsQuality,
)
from ...domain.network import NodeAggregate, TreeNode, aggregate_nodes, drilldown_tree, edge_stats
from ..deps import IndexedServices, Services
from ..errors import ApiProblem, problems

router = APIRouter(tags=["network"])

LevelParam = Annotated[NetworkLevel, Path(description="A level of the network, top to bottom.")]
CodeParam = Annotated[str, Path(description="The node's code at that level (any case).", examples=["DT-007"])]
# The anomaly rules that describe the data itself rather than the meter's behaviour.
INTEGRITY_RULES = ("duplicate_timestamps", "conflicting_duplicates", "missing_values", "gaps", "register_decrease")

_NOTE = (
    "Codes above the feeder are reused under several parents in the portal's data (for example one "
    "division under three circles), so the network is a graph, not a tree. Nodes are identified by "
    "(level, code); counts come from each meter's own path, so they never double-count. "
    "/v1/network/tree offers a drill-down view in which a reused code appears once per parent path."
)


def _status_by_dt(services: Services) -> dict[str, Counter[MeterStatus]]:
    return {
        dt: Counter({MeterStatus(s): n for s, n in counts.items()})
        for dt, counts in services.store.meter_status_by_transformer().items()
    }


def _nodes(services: Services) -> dict[tuple[NetworkLevel, str], NodeAggregate]:
    return aggregate_nodes(services.store.transformer_paths(), _status_by_dt(services))


def _overview(nodes: dict[tuple[NetworkLevel, str], NodeAggregate]) -> NetworkOverview:
    edges = [
        NetworkEdge(
            child_level=e.child_level,
            parent_level=e.parent_level,
            distinct_pairs=e.distinct_pairs,
            children_with_multiple_parents=e.children_with_multiple_parents,
            functional=e.functional,
        )
        for e in edge_stats(nodes)
    ]
    return NetworkOverview(
        is_tree=all(e.functional for e in edges),
        node_counts={level: sum(1 for (lvl, _) in nodes if lvl is level) for level in NetworkLevel},
        edges=edges,
        note=_NOTE,
    )


def _summary(node: NodeAggregate) -> NetworkNodeSummary:
    return NetworkNodeSummary(
        level=node.level,
        code=node.code,
        name=node.name,
        meter_count=node.meter_count,
        transformer_count=len(node.transformer_codes),
        parent_codes=sorted(node.parents),
    )


@router.get(
    "/v1/network",
    response_model=NetworkOverview,
    responses=problems(401, 422, 503),
    summary="Shape of the reconstructed network",
)
def network_overview(services: IndexedServices) -> NetworkOverview:
    return _overview(_nodes(services))


@router.get(
    "/v1/network/tree",
    response_model=list[NetworkTreeNode],
    responses=problems(401, 422, 503),
    summary="Drill-down tree of the network",
)
def network_tree(
    services: IndexedServices,
    depth: Annotated[int, Query(ge=1, le=7, description="How many levels to return, from zone down.")] = 7,
) -> list[NetworkTreeNode]:
    """Zone → … → transformer, built from the transformers' paths. A node means "this
    code under this path", so a code reused under several parents appears once under
    each, and the counts at every level add up to the fleet total.
    """

    def convert(node: TreeNode, remaining: int) -> NetworkTreeNode:
        return NetworkTreeNode(
            level=node.level,
            code=node.code,
            name=node.name,
            meter_count=node.meter_count,
            transformer_count=node.transformer_count,
            meters_by_status=dict(sorted(node.meters_by_status.items())),
            children=[convert(c, remaining - 1) for c in sorted(node.children.values(), key=lambda n: n.code)]
            if remaining > 1
            else [],
        )

    tree = drilldown_tree(services.store.transformer_paths(), _status_by_dt(services))
    return [convert(root, depth) for root in tree]


@router.get(
    "/v1/network/{level}",
    response_model=list[NetworkNodeSummary],
    responses=problems(401, 422, 503),
    summary="All nodes at one level",
)
def network_level(level: LevelParam, services: IndexedServices) -> list[NetworkNodeSummary]:
    nodes = _nodes(services)
    return [_summary(n) for (lvl, code), n in sorted(nodes.items(), key=lambda kv: kv[0][1]) if lvl is level]


@router.get(
    "/v1/network/{level}/{code}",
    response_model=NetworkNode,
    responses=problems(401, 404, 422, 503),
    summary="One network node",
)
def network_node(level: LevelParam, code: CodeParam, services: IndexedServices) -> NetworkNode:
    nodes = _nodes(services)
    node = nodes.get((level, code.strip().upper()))
    if node is None:
        raise ApiProblem(404, "network_node_not_found", f"No {level.value} with code {code!r}.")
    levels = list(NetworkLevel)
    parent_level = levels[levels.index(level) - 1] if level is not NetworkLevel.zone else None
    child_level = levels[levels.index(level) + 1] if level is not NetworkLevel.transformer else None

    def neighbours(links: dict, neighbour_level: NetworkLevel | None) -> list[NetworkNeighbour]:  # type: ignore[type-arg]
        if neighbour_level is None:
            return []
        return [
            NetworkNeighbour(
                code=c,
                name=nodes[(neighbour_level, c)].name,
                transformer_count=link.transformers,
                meter_count=link.meters,
            )
            for c, link in sorted(links.items())
        ]

    name_variants: list[str] = []
    if level is NetworkLevel.transformer:
        row = services.store.get_transformer(node.code)
        name_variants = json.loads(row["name_variants_json"]) if row else []
    return NetworkNode(
        **_summary(node).model_dump(),
        meters_by_status=dict(sorted(node.meters_by_status.items())),
        parents=neighbours(node.parents, parent_level),
        children=neighbours(node.children, child_level),
        transformers=sorted(node.transformer_codes),
        name_variants=name_variants,
    )


@router.get(
    "/v1/data-quality",
    response_model=DataQualityReport,
    responses=problems(401, 422, 503),
    tags=["data quality"],
    summary="Everything we had to correct or could not reconcile",
)
def data_quality(services: IndexedServices) -> DataQualityReport:
    meters = [
        MeterIssues(meter_id=meter_id, issues=[DataIssue.model_validate(i) for i in issues])
        for meter_id, issues in services.store.issues()
    ]
    by_code = Counter(i.code for m in meters for i in m.issues)
    # The readings section reuses the anomaly rules, so both reports always agree.
    rules = services.derived.rules()
    triggered = Counter(a.rule for result in rules.values() for a in result.anomalies)
    return DataQualityReport(
        meters_with_issues=len(meters),
        issues_by_code=dict(sorted(by_code.items())),
        meters=meters,
        network=_overview(_nodes(services)),
        readings=ReadingsQuality(meters_cached=len(rules), **{rule: triggered[rule] for rule in INTEGRITY_RULES}),
    )
