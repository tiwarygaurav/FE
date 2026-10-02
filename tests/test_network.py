"""Network reconstruction on the fixture subset of real meter records."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import replace

import pytest

from urja_api.domain.models import LEVELS, MeterStatus, NetworkLevel, NetworkNodeRef
from urja_api.domain.network import TreeNode, aggregate_nodes, drilldown_tree, edge_stats, reconcile
from urja_api.domain.normalize import meter_from_export

L = NetworkLevel
STALE_DT_007 = [("stale_name", L.transformer, "Old Malviya Nagar Xfmr", "Sanganer DT 7")]


@pytest.fixture
def network(meters, transformers):
    return reconcile(meters, transformers)


@pytest.fixture
def nodes(network, meters):
    status_by_dt: dict[str, Counter[MeterStatus]] = {}
    for m in meters:
        status_by_dt.setdefault(m.transformer_code, Counter())[m.status] += 1
    return aggregate_nodes(network.transformer_paths, status_by_dt), status_by_dt


def summarise(issues) -> list[tuple]:
    return [(i.code, i.level, i.reported, i.resolved) for i in issues]


def walk(nodes: list[TreeNode]) -> Iterator[TreeNode]:
    for node in nodes:
        yield node
        yield from walk(list(node.children.values()))


# ----------------------------------------------------------------------------- reconciliation


def test_blanks_and_stale_names_are_repaired_from_the_transformer(network):
    found = {meter_id: summarise(issues) for meter_id, issues in network.meter_issues.items() if issues}
    assert found == {
        "J100011": [("blank_code", L.circle, "Circle 6", "C-06")],
        "J100153": [("blank_code", L.substation, "Substation 16", "SS-16")],
        "J100162": [("blank_name", L.feeder, "F-003", "Feeder 3")],
        "J100218": [("blank_name", L.circle, "C-01", "Circle 1")],
        "J100400": STALE_DT_007,
        "J100401": STALE_DT_007,
        "J100402": STALE_DT_007,
    }
    assert network.unresolved == []


def test_meters_are_served_their_transformers_path(network):
    paths = network.meter_paths
    assert paths["J100011"].circle == NetworkNodeRef(code="C-06", name="Circle 6")
    assert paths["J100011"] == paths["J100051"] == network.transformer_paths["DT-012"]
    assert paths["J100162"].feeder == NetworkNodeRef(code="F-003", name="Feeder 3")
    assert paths["J100218"].circle == NetworkNodeRef(code="C-01", name="Circle 1")
    for meter_id in ("J100006", "J100400", "J100401", "J100402"):
        assert paths[meter_id].transformer == NetworkNodeRef(code="DT-007", name="Sanganer DT 7")


def test_the_dt_list_outranks_the_meters_majority(network):
    # Three of the four DT-007 meters in the subset carry the stale name; the DT list still wins.
    assert network.canonical_names[(L.transformer, "DT-007")] == "Sanganer DT 7"
    assert network.name_variants == {(L.transformer, "DT-007"): ["Old Malviya Nagar Xfmr"]}


def test_a_meter_disagreeing_with_its_transformer_gets_a_path_conflict(export_records, transformers):
    for record in export_records:
        if record["meterId"] == "J100040":  # on DT-001 with J100000 and J100200, which both say C-01
            record["hierarchy"]["circle"] = {"name": "Circle 2", "code": "C-02"}
    network = reconcile([meter_from_export(r) for r in export_records], transformers)
    assert summarise(network.meter_issues["J100040"]) == [("path_conflict", L.circle, "C-02", "C-01")]
    assert network.meter_paths["J100040"].circle.code == "C-01"
    assert len(network.unresolved) == 1
    assert "DT-001" in network.unresolved[0] and "circle" in network.unresolved[0]


def test_the_dt_list_decides_the_feeder(meters, transformers):
    moved = [replace(t, feeder_code="F-099") if t.code == "DT-001" else t for t in transformers]
    network = reconcile(meters, moved)
    assert network.transformer_paths["DT-001"].feeder.code == "F-099"
    for meter_id in ("J100000", "J100040", "J100200"):
        assert summarise(network.meter_issues[meter_id]) == [("path_conflict", L.feeder, "F-001", "F-099")]


def test_a_transformer_missing_from_the_dt_list_is_reported(meters, transformers):
    network = reconcile(meters, [t for t in transformers if t.code != "DT-034"])
    assert network.unresolved == ["transformer DT-034 is referenced by meters but missing from the DT list"]
    assert network.transformer_paths["DT-034"].transformer == NetworkNodeRef(code="DT-034", name="Raja Park DT 34")


# ----------------------------------------------------------------------------- nodes and edges


def test_every_level_counts_every_meter_once(network, nodes, meters):
    aggregates, _ = nodes
    for level in LEVELS:
        at_level = [n for (lvl, _), n in aggregates.items() if lvl is level]
        assert sum(n.meter_count for n in at_level) == len(meters), level
        assert sum(len(n.transformer_codes) for n in at_level) == len(network.transformer_paths), level


def test_a_division_under_three_circles(nodes):
    aggregates, _ = nodes
    d01 = aggregates[(L.division, "D-01")]
    assert d01.meter_count == 5
    assert d01.transformer_codes == {"DT-001", "DT-011", "DT-021"}
    assert {code: (link.transformers, link.meters) for code, link in d01.parents.items()} == {
        "C-01": (1, 3),
        "C-03": (1, 1),
        "C-05": (1, 1),
    }
    assert set(d01.children) == {"SD-01", "SD-07", "SD-11"}
    assert aggregates[(L.transformer, "DT-007")].meters_by_status == Counter(
        {MeterStatus.installed: 3, MeterStatus.decommissioned: 1}
    )


def test_edge_stats_show_which_steps_are_not_a_tree(nodes):
    aggregates, _ = nodes
    edges = {e.child_level: e for e in edge_stats(aggregates)}
    assert {level: e.functional for level, e in edges.items()} == {
        L.circle: True,
        L.division: False,  # D-01 (3 circles), D-02 (2)
        L.subdivision: False,  # SD-05, SD-07
        L.substation: False,  # SS-01, SS-03
        L.feeder: True,
        L.transformer: True,
    }
    assert [e.children_with_multiple_parents for e in edges.values()] == [0, 2, 2, 2, 0, 0]
    assert edges[L.division].distinct_pairs == 11  # 8 divisions, D-01 counted 3x and D-02 2x
    assert all(e.parent_level is LEVELS[LEVELS.index(level) - 1] for level, e in edges.items())


def test_drilldown_totals_add_up(network, nodes, meters):
    _, status_by_dt = nodes
    roots = drilldown_tree(network.transformer_paths, status_by_dt)
    assert [r.code for r in roots] == ["Z-01", "Z-02", "Z-03"]
    assert sum(r.meter_count for r in roots) == len(meters)
    assert sum(r.transformer_count for r in roots) == len(network.transformer_paths)
    for node in walk(roots):
        children = list(node.children.values())
        if children:
            assert node.meter_count == sum(c.meter_count for c in children)
            assert node.transformer_count == sum(c.transformer_count for c in children)
            assert node.meters_by_status == sum((c.meters_by_status for c in children), Counter())
        else:
            assert node.level is L.transformer and node.transformer_count == 1
    # A reused code appears once per parent path, and the occurrences add up to its total.
    d01 = [n for n in walk(roots) if n.level is L.division and n.code == "D-01"]
    assert sorted(n.meter_count for n in d01) == [1, 1, 3]
