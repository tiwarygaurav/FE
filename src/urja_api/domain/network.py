"""Reconstruct the distribution network from what meters report about themselves.

The portal has no network endpoint. Each meter record carries its own copy of the path
Zone › Circle › Division › Subdivision › Sub Station › Feeder › DT, and those copies are
messy: some levels have a blank code or a blank name, a few meters carry a stale
transformer name, and the codes above the feeder are reused under different parents
(for example D-01 appears under circles C-01, C-03 and C-05).

What *is* consistent is the transformer. Every meter on the same DT reports the same
path, and the DT master list (``/portal/dts``) agrees with them on the feeder. So the
reconstruction is anchored on the DT:

1. canonical names: the DT master list for transformers; for other levels the most
   common non-blank name reported with each code;
2. for every DT, the consensus code at each level above it (majority of its meters,
   master-list feeder first); a blank code is resolved from that consensus or, failing
   that, by looking its name up among the canonical names;
3. every meter inherits its DT's path, and each disagreement between what it reported
   and what we serve becomes a `DataIssue` (`blank_code`, `blank_name`, `stale_name`,
   `path_conflict`).

The result is *not* a tree above the feeder. Each level turns out to be assigned to DTs
independently, so D-01 really does sit under three circles and F-001 under two
substations. Rather than invent single parents by majority vote (that would rewrite most
meters' paths on coin-flip ties), nodes are identified by `(level, code)`, list every
parent they were observed under, and have counts computed from the meters' own codes.
Walking child lists top-down would count meters several times over. For browsing there
is also a drill-down view: a trie of the DT paths, where the same code can appear under
several parents but counts add up.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import pairwise

from .models import LEVELS, DataIssue, MeterStatus, NetworkLevel, NetworkNodeRef, NetworkPath
from .normalize import MeterRecord, TransformerRecord

UPPER_LEVELS = LEVELS[:-1]  # zone .. feeder


@dataclass
class ReconciledNetwork:
    transformer_paths: dict[str, NetworkPath]
    meter_paths: dict[str, NetworkPath]
    meter_issues: dict[str, list[DataIssue]]
    canonical_names: dict[tuple[NetworkLevel, str], str]
    name_variants: dict[tuple[NetworkLevel, str], list[str]]
    unresolved: list[str] = field(default_factory=list)  # human-readable problems we could not fix


def _most_common(values: Iterable[str]) -> str | None:
    counts = Counter(values)
    if not counts:
        return None
    # Highest count wins; ties are broken alphabetically so results are deterministic.
    return min(counts, key=lambda v: (-counts[v], v))


def reconcile(meters: list[MeterRecord], transformers: list[TransformerRecord]) -> ReconciledNetwork:
    master = {t.code: t for t in transformers}

    # 1. canonical names per (level, code)
    names: dict[tuple[NetworkLevel, str], Counter[str]] = defaultdict(Counter)
    for m in meters:
        for level, node in m.reported_path.items():
            if node.code and node.name:
                names[(level, node.code)][node.name] += 1
    canonical: dict[tuple[NetworkLevel, str], str] = {
        key: _most_common(counter.elements())
        for key, counter in names.items()  # type: ignore[misc]
    }
    for t in transformers:
        canonical[(NetworkLevel.transformer, t.code)] = t.name
    by_name: dict[tuple[NetworkLevel, str], str] = {(lvl, name): code for (lvl, code), name in canonical.items()}

    def resolve_blank_code(level: NetworkLevel, name: str | None) -> str | None:
        return by_name.get((level, name)) if name else None

    # 2. consensus path per transformer
    meters_by_dt: dict[str, list[MeterRecord]] = defaultdict(list)
    for m in meters:
        meters_by_dt[m.transformer_code].append(m)

    unresolved: list[str] = []
    transformer_paths: dict[str, NetworkPath] = {}
    for dt_code, dt_meters in sorted(meters_by_dt.items()):
        path: dict[NetworkLevel, NetworkNodeRef] = {}
        for level in UPPER_LEVELS:
            reported = []
            for m in dt_meters:
                node = m.reported_path[level]
                code = node.code or resolve_blank_code(level, node.name)
                if code:
                    reported.append(code)
            code = _most_common(reported)
            if level is NetworkLevel.feeder and dt_code in master and master[dt_code].feeder_code:
                code = master[dt_code].feeder_code
            if len(set(reported)) > 1:
                unresolved.append(
                    f"transformer {dt_code}: meters disagree on {level.value} ({dict(Counter(reported))}); using {code}"
                )
            if code is None:
                unresolved.append(f"transformer {dt_code}: no {level.value} code reported by any meter")
                code = "UNKNOWN"
            path[level] = NetworkNodeRef(code=code, name=canonical.get((level, code)))
        dt_name = canonical.get((NetworkLevel.transformer, dt_code))
        if dt_code not in master:
            unresolved.append(f"transformer {dt_code} is referenced by meters but missing from the DT list")
        path[NetworkLevel.transformer] = NetworkNodeRef(code=dt_code, name=dt_name)
        transformer_paths[dt_code] = NetworkPath(**{lvl.value: ref for lvl, ref in path.items()})

    # 3. per-meter paths and issues
    meter_paths: dict[str, NetworkPath] = {}
    meter_issues: dict[str, list[DataIssue]] = {}
    variants: dict[tuple[NetworkLevel, str], set[str]] = defaultdict(set)
    for m in meters:
        served = transformer_paths[m.transformer_code]
        meter_paths[m.meter_id] = served
        issues = [
            DataIssue(code="unrecognised_value", message=f"Portal sent an unrecognised {f} value", reported=raw)
            for f, raw in m.vocabulary_issues
        ]
        for level in LEVELS:
            node = m.reported_path[level]
            ref: NetworkNodeRef = getattr(served, level.value)
            if not node.code:
                issues.append(
                    DataIssue(
                        code="blank_code",
                        level=level,
                        message=f"Portal reported no {level.value} code; derived from the meter's transformer.",
                        reported=node.name,
                        resolved=ref.code,
                    )
                )
            elif node.code != ref.code:
                issues.append(
                    DataIssue(
                        code="path_conflict",
                        level=level,
                        message=f"Meter reports a different {level.value} than other meters on its transformer.",
                        reported=node.code,
                        resolved=ref.code,
                    )
                )
            if not node.name:
                issues.append(
                    DataIssue(
                        code="blank_name",
                        level=level,
                        message=f"Portal reported no {level.value} name.",
                        reported=node.code,
                        resolved=ref.name,
                    )
                )
            elif ref.name and node.name != ref.name and node.code in (None, ref.code):
                variants[(level, ref.code)].add(node.name)
                issues.append(
                    DataIssue(
                        code="stale_name",
                        level=level,
                        message=f"Portal reported an outdated {level.value} name.",
                        reported=node.name,
                        resolved=ref.name,
                    )
                )
        meter_issues[m.meter_id] = issues

    return ReconciledNetwork(
        transformer_paths=transformer_paths,
        meter_paths=meter_paths,
        meter_issues=meter_issues,
        canonical_names=canonical,
        name_variants={key: sorted(v) for key, v in variants.items()},
        unresolved=unresolved,
    )


# ----------------------------------------------------------------------------- nodes & edges


@dataclass
class Link:
    """How often a node was observed next to a neighbouring node."""

    transformers: int = 0
    meters: int = 0


@dataclass
class NodeAggregate:
    level: NetworkLevel
    code: str
    name: str | None
    transformer_codes: set[str] = field(default_factory=set)
    meters_by_status: Counter[MeterStatus] = field(default_factory=Counter)
    parents: dict[str, Link] = field(default_factory=dict)
    children: dict[str, Link] = field(default_factory=dict)

    @property
    def meter_count(self) -> int:
        return sum(self.meters_by_status.values())


def aggregate_nodes(
    transformer_paths: dict[str, NetworkPath], status_by_dt: dict[str, Counter[MeterStatus]]
) -> dict[tuple[NetworkLevel, str], NodeAggregate]:
    """Every (level, code) node with global counts and all observed parents/children.

    Counts come from grouping transformers (and so meters) on their own codes, so a
    meter is counted exactly once per level.
    """
    nodes: dict[tuple[NetworkLevel, str], NodeAggregate] = {}
    for dt_code, path in transformer_paths.items():
        statuses = status_by_dt.get(dt_code, Counter())
        meters = sum(statuses.values())
        refs = [getattr(path, level.value) for level in LEVELS]
        for level, ref in zip(LEVELS, refs, strict=True):
            node = nodes.setdefault((level, ref.code), NodeAggregate(level=level, code=ref.code, name=ref.name))
            node.transformer_codes.add(dt_code)
            node.meters_by_status.update(statuses)
        for (parent_level, parent), (child_level, child) in pairwise(zip(LEVELS, refs, strict=True)):
            up = nodes[(child_level, child.code)].parents.setdefault(parent.code, Link())
            down = nodes[(parent_level, parent.code)].children.setdefault(child.code, Link())
            for link in (up, down):
                link.transformers += 1
                link.meters += meters
    return nodes


@dataclass(frozen=True)
class EdgeStats:
    child_level: NetworkLevel
    parent_level: NetworkLevel
    distinct_pairs: int
    children_with_multiple_parents: int

    @property
    def functional(self) -> bool:
        """True if every child has exactly one parent (the edge behaves like a tree)."""
        return self.children_with_multiple_parents == 0


def edge_stats(nodes: dict[tuple[NetworkLevel, str], NodeAggregate]) -> list[EdgeStats]:
    stats = []
    for parent_level, child_level in pairwise(LEVELS):
        children = [n for (level, _), n in nodes.items() if level is child_level]
        stats.append(
            EdgeStats(
                child_level=child_level,
                parent_level=parent_level,
                distinct_pairs=sum(len(n.parents) for n in children),
                children_with_multiple_parents=sum(1 for n in children if len(n.parents) > 1),
            )
        )
    return stats


# ----------------------------------------------------------------------------- drill-down view


@dataclass
class TreeNode:
    level: NetworkLevel
    code: str
    name: str | None
    meter_count: int = 0
    transformer_count: int = 0
    meters_by_status: Counter[MeterStatus] = field(default_factory=Counter)
    children: dict[str, TreeNode] = field(default_factory=dict)


def drilldown_tree(
    transformer_paths: dict[str, NetworkPath], status_by_dt: dict[str, Counter[MeterStatus]]
) -> list[TreeNode]:
    """A trie of transformer paths, for top-down browsing.

    A node here means "this code *under this path*", so a code reused across parents
    (e.g. D-01) shows up once under each of them, and every node's counts cover exactly
    the transformers below it. Totals therefore add up at every level.
    """
    roots: dict[str, TreeNode] = {}
    for dt_code, path in sorted(transformer_paths.items()):
        statuses = status_by_dt.get(dt_code, Counter())
        siblings = roots
        for level in LEVELS:
            ref: NetworkNodeRef = getattr(path, level.value)
            node = siblings.get(ref.code)
            if node is None:
                node = siblings[ref.code] = TreeNode(level=level, code=ref.code, name=ref.name)
            node.meter_count += sum(statuses.values())
            node.transformer_count += 1
            node.meters_by_status.update(statuses)
            siblings = node.children
    return sorted(roots.values(), key=lambda n: n.code)
