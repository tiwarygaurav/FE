"""The public data model of the API.

These types *are* the contract: FastAPI renders them into ``openapi.json``. They are
deliberately independent of the portal's shapes (camelCase keys, strings for numbers,
``DD/MM/YYYY`` timestamps, two nameplate formats, ``"Name (CODE)"`` labels...), which are
translated in `normalize.py`.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Severity = Literal["info", "warning", "error"]
AnomalyRule = Literal[
    "duplicate_timestamps",
    "conflicting_duplicates",
    "missing_values",
    "gaps",
    "register_decrease",
    "implausible_load",
    "flatline",
    "consumption_surge",
    "power_factor_above_one",
    "no_voltage",
    "voltage_outside_10pct",
    "voltage_outside_6pct",
    "decommissioned_reporting",
    "stale",
]
DataIssueCode = Literal["blank_code", "blank_name", "stale_name", "path_conflict", "unrecognised_value"]
ReadingFlag = Literal[
    "missing_kwh", "missing_kvah", "missing_voltage", "register_decrease", "gap_before", "duplicate",
    "conflicting_duplicate",
]  # fmt: skip
Granularity = Literal["day", "hour"]
GroupBy = Literal[
    "zone", "circle", "division", "subdivision", "substation", "feeder", "transformer",
    "status", "make", "phase", "installation_type", "meter",
]  # fmt: skip

# `unknown` is only used if the portal starts sending a value we have never seen; the
# meter is still served, with a `data_issues` entry carrying the raw value.


class MeterStatus(StrEnum):
    installed = "installed"
    faulty = "faulty"
    decommissioned = "decommissioned"
    unknown = "unknown"


class Phase(StrEnum):
    single = "single"
    three = "three"
    unknown = "unknown"


class InstallationType(StrEnum):
    whole_current = "whole_current"
    ct_operated = "ct_operated"
    unknown = "unknown"


class NetworkLevel(StrEnum):
    """Levels of the distribution network, top to bottom."""

    zone = "zone"
    circle = "circle"
    division = "division"
    subdivision = "subdivision"
    substation = "substation"
    feeder = "feeder"
    transformer = "transformer"


LEVELS: tuple[NetworkLevel, ...] = tuple(NetworkLevel)


class Location(BaseModel):
    """WGS84 (assumed; the portal states no datum). Rounded to 6 decimal places (~0.1 m):
    the portal sends 15, which is false precision."""

    latitude: float = Field(examples=[26.938961])
    longitude: float = Field(examples=[75.830957])

    @field_validator("latitude", "longitude")
    @classmethod
    def _round(cls, value: float) -> float:
        return round(value, 6)


class NetworkNodeRef(BaseModel):
    code: str = Field(examples=["DT-001"])
    name: str | None = Field(default=None, examples=["Malviya Nagar DT 1"])


class NetworkPath(BaseModel):
    """Where a meter sits in the network, from zone down to its distribution transformer."""

    zone: NetworkNodeRef
    circle: NetworkNodeRef
    division: NetworkNodeRef
    subdivision: NetworkNodeRef
    substation: NetworkNodeRef
    feeder: NetworkNodeRef
    transformer: NetworkNodeRef


class DataIssue(BaseModel):
    """Something in the portal's data that we corrected or could not reconcile."""

    code: DataIssueCode = Field(examples=["stale_name"])
    level: NetworkLevel | None = None
    message: str
    reported: str | None = Field(default=None, description="What the portal reported.")
    resolved: str | None = Field(default=None, description="What this API serves instead.")


class MeterSummary(BaseModel):
    meter_id: str = Field(examples=["J100000"])
    serial_number: str = Field(examples=["SE33962"])
    make: str = Field(examples=["HPL"])
    phase: Phase
    status: MeterStatus
    installation_type: InstallationType
    transformer_code: str = Field(examples=["DT-001"])
    feeder_code: str = Field(examples=["F-001"])
    location: Location | None = None


class MeterListItem(MeterSummary):
    data_issue_count: int = Field(description="How many corrections this API applied to the meter's portal data.")
    reading_interval_minutes: int | None = Field(
        default=None,
        description="30 for half-hourly meters, 1440 for daily ones; null until the meter's readings are cached.",
    )
    distance_km: float | None = Field(
        default=None, description="Distance from the search point; set only on proximity searches (`near=`)."
    )


class ReadingsCoverage(BaseModel):
    """What the local cache currently holds for a meter's readings."""

    interval_minutes: int | None = Field(
        description="Detected reading interval, e.g. 30 (half-hourly) or 1440 (daily)."
    )
    count: int
    first_reading_at: datetime | None
    last_reading_at: datetime | None
    fetched_at: datetime = Field(description="When the series was last fetched from the portal.")


class TransformerSummary(BaseModel):
    code: str = Field(examples=["DT-001"])
    name: str = Field(examples=["Malviya Nagar DT 1"])
    feeder_code: str = Field(examples=["F-001"])
    capacity_kva: float | None = Field(default=None, examples=[100])


class Meter(MeterSummary):
    network: NetworkPath
    transformer: TransformerSummary | None = None
    data_issues: list[DataIssue] = Field(default_factory=list)
    readings_coverage: ReadingsCoverage | None = Field(
        default=None, description="What the readings cache holds for this meter; null until it has been fetched."
    )
    synced_at: datetime = Field(description="When this record was last checked against the portal by a sync.")


class Transformer(TransformerSummary):
    network: NetworkPath = Field(description="The transformer's own position in the network (zone → feeder).")
    meter_count: int
    meters_by_status: dict[MeterStatus, int]
    name_variants: list[str] = Field(
        default_factory=list, description="Other names meters report for this transformer (stale data)."
    )


class Page[T](BaseModel):
    items: list[T]
    total: int = Field(description="Number of items matching the query (ignoring limit/offset).")
    limit: int
    offset: int


# ----------------------------------------------------------------------------- readings


class Reading(BaseModel):
    timestamp: datetime = Field(description="Reading time, ISO 8601 with the IST offset (+05:30).")
    register_kwh: float | None = Field(
        description="Active-energy register (kWh): a running total, not consumption. 0.01 kWh resolution."
    )
    register_kvah: float | None = Field(description="Apparent-energy register (kVAh): a running total.")
    voltage_v: float | None = Field(description="R-phase voltage at the moment of reading (V), a spot value.")
    consumption_kwh: float | None = Field(
        description="Active energy used since the previous reading in this response: the register's rise "
        "above its highest value so far (over the meter's whole series, not just this window), so a read a "
        "rounding step low adds nothing. Null for the first "
        "reading, when either register value is missing, or when the register went down (flagged "
        "`register_decrease`). After a gap it covers the whole gap (flagged `gap_before`)."
    )
    flags: list[ReadingFlag] = Field(default_factory=list, description="Quality flags for this reading.")


class Freshness(BaseModel):
    fetched_at: datetime | None = Field(description="When the data was fetched from the portal.")
    stale: bool = Field(
        description="True when the data could not be refreshed just now (the portal failed, or this service's "
        "own request queue was full) and a cached copy older than the freshness TTL was served."
    )


class ReadingsSummary(BaseModel):
    count: int
    interval_minutes: int | None = Field(description="Detected reading interval in minutes.")
    consumption_kwh: float | None = Field(
        description="Register difference between the first and last reading in this response, i.e. the "
        "energy used between those two instants (blank values in between lose nothing). Null if the "
        "register went down between two readings in this response (see `flags.register_decrease`; a "
        "decrease flagged on the first reading that carries a value is a fall from before the window and "
        "does not count). For calendar totals that include the reading closing the last day, use "
        "`/consumption`."
    )
    average_power_factor: float | None = Field(
        description="ΔkWh / ΔkVAh over the same span. Null when the apparent energy is below 1 kVAh, where "
        "the registers' 0.01 resolution would distort the ratio by 1 % or more."
    )
    min_voltage_v: float | None
    max_voltage_v: float | None
    missing_intervals: int = Field(
        description="Readings missing between consecutive readings in this response, given the interval. "
        "Readings missing before the first or after the last one returned are not counted; the `gaps` rule "
        "of `/anomalies` covers the whole series."
    )
    flags: dict[ReadingFlag, int] = Field(default_factory=dict, description="Count of readings per quality flag.")


class ReadingsResponse(BaseModel):
    meter_id: str
    start: datetime | None = Field(description="Start of the window served (inclusive).")
    end: datetime | None = Field(description="End of the window served (inclusive).")
    last_reading_at: datetime | None = Field(
        description="The meter's latest reading overall. Defaults are relative to this, not to the current "
        "time, so compare it with today before assuming 'recent' means this week."
    )
    summary: ReadingsSummary
    readings: list[Reading]
    freshness: Freshness


# ----------------------------------------------------------------------------- network


class NetworkNeighbour(BaseModel):
    code: str
    name: str | None
    transformer_count: int = Field(description="Transformers through which the two nodes are connected.")
    meter_count: int


class NetworkNodeSummary(BaseModel):
    level: NetworkLevel
    code: str
    name: str | None
    meter_count: int
    transformer_count: int
    parent_codes: list[str] = Field(
        description="Every parent this code was observed under: one for circles and transformers, usually "
        "several for the levels in between (see /v1/network)."
    )


class NetworkNode(NetworkNodeSummary):
    meters_by_status: dict[MeterStatus, int]
    parents: list[NetworkNeighbour]
    children: list[NetworkNeighbour]
    transformers: list[str] = Field(
        description="Codes of the distribution transformers under this node. A transformer node lists "
        "itself; its details are at /v1/transformers/{code}."
    )
    name_variants: list[str] = Field(
        default_factory=list, description="Other names the portal uses for this node (stale data)."
    )


class NetworkEdge(BaseModel):
    child_level: NetworkLevel
    parent_level: NetworkLevel
    distinct_pairs: int
    children_with_multiple_parents: int
    functional: bool = Field(description="True if every child has exactly one parent at this step.")


class NetworkOverview(BaseModel):
    is_tree: bool = Field(description="False if any level has children appearing under several parents.")
    node_counts: dict[NetworkLevel, int]
    edges: list[NetworkEdge]
    note: str


class NetworkTreeNode(BaseModel):
    level: NetworkLevel
    code: str
    name: str | None
    meter_count: int
    transformer_count: int
    meters_by_status: dict[MeterStatus, int]
    children: list[NetworkTreeNode] = Field(default_factory=list)


# ----------------------------------------------------------------------------- quality & status


class MeterIssues(BaseModel):
    meter_id: str
    issues: list[DataIssue]


class ReadingsQuality(BaseModel):
    """How many meters trigger each data-integrity anomaly rule (see `/v1/insights/anomalies`),
    over the readings in the local cache."""

    meters_cached: int = Field(description="Meters whose readings are cached; the counts below cover only these.")
    duplicate_timestamps: int
    conflicting_duplicates: int
    missing_values: int = Field(description="Meters with a blank kWh register or voltage in some reading.")
    gaps: int
    register_decrease: int


class DataQualityReport(BaseModel):
    meters_with_issues: int
    issues_by_code: dict[DataIssueCode, int]
    meters: list[MeterIssues]
    network: NetworkOverview
    readings: ReadingsQuality


class ConsumptionBucket(BaseModel):
    start: datetime
    end: datetime
    consumption_kwh: float | None = Field(description="Energy used in the bucket; null if it cannot be computed.")
    apparent_kvah: float | None
    power_factor: float | None = Field(
        description="kWh / kVAh for the bucket. Null when the apparent energy is below 1 kVAh, where the "
        "registers' 0.01 resolution would distort the ratio (in practice: hourly buckets)."
    )
    coverage: float = Field(description="Share of the bucket covered by consecutive readings (0-1).")
    complete: bool = Field(description="True if readings exist at both bucket boundaries with nothing odd between.")


class ConsumptionResponse(BaseModel):
    meter_id: str
    granularity: Granularity
    start: datetime | None = Field(
        description="Start of the first bucket. Buckets cover every hour/day the window touches, as far as the "
        "cached readings reach; null when the window holds no data."
    )
    end: datetime | None = Field(description="End of the last bucket (exclusive).")
    last_reading_at: datetime | None
    total_kwh: float | None = Field(
        description="Register difference across the buckets' span, measured between the first and last "
        "readings inside it: exact even when readings are missing in between, but null if the register "
        "went down. May exceed the sum of `consumption_kwh` when an interval crosses a bucket boundary "
        "because of a gap."
    )
    buckets: list[ConsumptionBucket]
    freshness: Freshness


# ----------------------------------------------------------------------------- insights


class AnomalyOut(BaseModel):
    rule: AnomalyRule
    severity: Severity
    message: str
    occurrences: int
    first_at: datetime | None
    last_at: datetime | None


class MeterAnomalies(BaseModel):
    meter_id: str
    status: MeterStatus
    transformer_code: str
    anomalies: list[AnomalyOut]


class AnomalyReport(BaseModel):
    meters_analysed: int = Field(description="Meters whose readings are in the local cache.")
    meters_total: int
    meters_by_rule: dict[AnomalyRule, int] = Field(description="How many meters trigger each rule.")
    rule_severity: dict[AnomalyRule, Severity] = Field(description="Severity of each rule in `meters_by_rule`.")
    meters_by_severity: dict[Severity, int] = Field(
        description="Distinct meters with at least one anomaly of each severity."
    )
    meters: list[MeterAnomalies] = Field(
        description="Meters with anomalies. Meters whose only anomaly is `stale` are left out unless `rule=stale` "
        "is asked for; they still count in `meters_by_rule`."
    )


class ConsumptionGroup(BaseModel):
    key: str = Field(examples=["DT-001"])
    name: str | None = None
    meter_count: int = Field(description="Meters in the group.")
    meters_with_data_count: int = Field(description="Those with at least one complete day in the window.")
    consumption_kwh: float
    share: float = Field(description="Share of the total across all groups (0-1).")


class ConsumptionInsight(BaseModel):
    group_by: GroupBy
    start: datetime | None = Field(
        description="Midnight (IST) starting the first day of the window; null when there is no window "
        "because no readings are cached yet."
    )
    end: datetime | None = Field(description="Midnight (IST) after the last day of the window (exclusive).")
    days_counted: int = Field(description="Distinct complete IST days that contributed to the totals.")
    first_day: date | None = Field(description="First complete day counted.")
    last_day: date | None = Field(description="Last complete day counted.")
    include_decommissioned: bool
    meters_analysed: int = Field(description="Meters with cached readings that were included.")
    meters_total: int
    total_kwh: float | None = Field(description="Null when no meter with cached readings was included.")
    groups_total: int = Field(description="Number of groups; `groups` holds the top `limit` by consumption.")
    groups: list[ConsumptionGroup]
    note: str
