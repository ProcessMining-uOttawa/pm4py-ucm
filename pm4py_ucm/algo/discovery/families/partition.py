"""Partition an event log by 1–2 case-level attributes.

The partitioner reuses the attribute machinery of
:mod:`pm4py_ucm.algo.discovery.scenarios.decision_mining`:
:func:`~pm4py_ucm.algo.discovery.scenarios.decision_mining.extract_case_features`
already detects case-constant attributes (including event-level columns
that real-world XES exports repeat on every event), classifies each into
a jUCMNav-compatible type (``boolean`` / ``integer`` / ``enumeration``)
and sanitises names and enumeration values into legal jUCMNav
identifiers. This module layers the *grouping* semantics on top:

* enumeration attributes partition by value, with the lowest-count
  values merged into an ``Other`` bucket when the cardinality exceeds
  ``max_values_per_attribute``;
* boolean attributes partition into ``true`` / ``false``;
* integer attributes are **binned** — either on explicit ``bin_edges``
  or on quantiles — because raw numeric values (ages, amounts) almost
  never repeat often enough to partition on directly;
* cases with a missing value get an ``Unknown`` bucket (or are dropped
  when ``unknown_bucket=False``).

The result is a :class:`Partition`: an ordered list of
:class:`PartitionCell` objects (one per observed value combination with
at least ``min_cases`` cases, each carrying its slice of the log) plus
the per-attribute value axes needed to lay the cells out on a grid.
Everything is deterministic — values are ordered by sorted raw value
(ranges ascending; ``Other`` and ``Unknown`` last), so repeated runs on
the same log produce identical partitions, identical map names, and
therefore byte-identical exports downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..scenarios.decision_mining import (
    DEFAULT_NUMERIC_COERCION_THRESHOLD,
    AttributeSpec,
    _sanitise_jucmnav_name,
    extract_case_features,
)


#: Display label / kind markers of the synthetic buckets.
OTHER_LABEL = "Other"
UNKNOWN_LABEL = "Unknown"

#: Sentinel marking a case whose attribute value was excluded by an
#: ``include_values`` filter — dropped from the partition (and counted
#: in ``dropped_cases``), distinct from a *missing* value (``None``),
#: which the Unknown bucket may still absorb.
_EXCLUDED = object()


# ---------------------------------------------------------------------------
# Value / attribute / cell containers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PartitionValue:
    """One value on a partition axis.

    Attributes
    ----------
    label
        Display form — the raw log value for enumerations
        (``"Breast"``), ``"true"`` / ``"false"`` for booleans, a
        ``"18-39"`` range for binned integers, or the synthetic
        ``"Other"`` / ``"Unknown"``.
    token
        The jUCMNav identifier this value contributes to expressions
        and enumeration types (``"Breast"``, ``"_18_39"``). Sanitised
        via the decision-mining rules (leading digits get an
        underscore).
    kind
        ``"value"`` (a single raw value), ``"range"`` (an integer
        bin), ``"other"`` (the merged low-count bucket), or
        ``"unknown"`` (missing values).
    raw_values
        The raw log values covered — one entry for ``"value"``, every
        merged raw value for ``"other"``, empty for ranges/unknown.
    lo / hi
        Inclusive numeric bounds for ``"range"`` values; ``None``
        otherwise.
    """

    label: str
    token: str
    kind: str = "value"
    raw_values: Tuple[str, ...] = ()
    lo: Optional[float] = None
    hi: Optional[float] = None


@dataclass
class PartitionAttribute:
    """One partition axis: the underlying attribute plus its value list.

    ``values`` is the ordered axis — every value that at least one case
    maps to, in deterministic display order. ``binned`` is ``True``
    when the attribute is an integer partitioned into ranges (in which
    case the umbrella assembler creates an *enumeration* variable named
    ``<name>_group`` rather than an integer variable)."""

    spec: AttributeSpec
    values: List[PartitionValue] = field(default_factory=list)
    binned: bool = False

    @property
    def source_name(self) -> str:
        """Original column name in the log."""
        return self.spec.source_name

    @property
    def display_name(self) -> str:
        """Human-facing name — the source column without a ``case:`` prefix."""
        n = self.spec.source_name
        return n[len("case:"):] if n.startswith("case:") else n

    @property
    def variable_name(self) -> str:
        """jUCMNav variable name the umbrella assembler will use."""
        if self.binned:
            return self.spec.jucmnav_name + "_group"
        return self.spec.jucmnav_name


@dataclass
class PartitionCell:
    """One cell — a value combination and the cases that fall in it."""

    values: Tuple[PartitionValue, ...]
    case_ids: List[str]
    df: Any  # pandas.DataFrame slice of the original log

    @property
    def n_cases(self) -> int:
        return len(self.case_ids)

    @property
    def labels(self) -> Tuple[str, ...]:
        return tuple(v.label for v in self.values)

    @property
    def label(self) -> str:
        """Display label — ``"Breast"`` or ``"Breast / 40-59"``."""
        return " / ".join(self.labels)


@dataclass
class Partition:
    """Result of :func:`partition_log`.

    ``cells`` holds every observed value combination with at least
    ``min_cases`` cases, ordered by position on the value axes.
    ``skipped_cells`` records combinations that were observed but fell
    below ``min_cases`` — they are excluded from mining but should be
    displayed (grayed) so the grid shape stays interpretable.
    ``dropped_cases`` counts cases excluded entirely (missing value
    with ``unknown_bucket=False``, or a trimmed enumeration value with
    ``other_bucket=False``)."""

    attributes: List[PartitionAttribute]
    cells: List[PartitionCell]
    skipped_cells: List[Tuple[Tuple[PartitionValue, ...], int]]
    total_cases: int
    dropped_cases: int
    case_id_col: str

    @property
    def covered_cases(self) -> int:
        return sum(c.n_cases for c in self.cells)

    def grid_counts(self) -> Dict[Tuple[str, ...], int]:
        """``{labels: n_cases}`` for every observed combination,
        including the skipped ones — the coverage-heatmap input."""
        out: Dict[Tuple[str, ...], int] = {}
        for c in self.cells:
            out[c.labels] = c.n_cases
        for values, n in self.skipped_cells:
            out[tuple(v.label for v in values)] = n
        return out


# ---------------------------------------------------------------------------
# Attribute detection
# ---------------------------------------------------------------------------

def detect_case_attributes(
    log_df,
    case_id_col: str = "case:concept:name",
    max_enum_cardinality: int = 40,
    numeric_coercion_threshold: float = DEFAULT_NUMERIC_COERCION_THRESHOLD,
):
    """Detect the case-constant attributes of ``log_df`` usable as
    partition axes.

    Thin wrapper over
    :func:`~pm4py_ucm.algo.discovery.scenarios.decision_mining.extract_case_features`
    keeping only what partitioning needs.

    Returns
    -------
    (attribute_specs, per_case_raw)
        ``attribute_specs`` maps each sanitised jUCMNav name to its
        :class:`AttributeSpec`; ``per_case_raw`` is a DataFrame indexed
        by case ID with one column per surviving source attribute
        (raw, unencoded values). Returns ``({}, None)`` when the log
        carries no usable case-constant attribute.
    """
    _, specs, _, per_case_raw = extract_case_features(
        log_df,
        case_id_col=case_id_col,
        max_enum_cardinality=max_enum_cardinality,
        numeric_coercion_threshold=numeric_coercion_threshold,
    )
    return specs or {}, per_case_raw


def _resolve_spec(
    name: str, specs: Dict[str, AttributeSpec],
) -> AttributeSpec:
    """Find the spec for a user-supplied attribute name — accepts the
    source column name (``"case:cancer_type"`` / ``"cancer_type"``) or
    the sanitised jUCMNav name."""
    for spec in specs.values():
        if name in (spec.source_name, spec.jucmnav_name):
            return spec
    for spec in specs.values():  # tolerate a bare name for a case: column
        if spec.source_name == f"case:{name}":
            return spec
    available = sorted(s.source_name for s in specs.values())
    raise ValueError(
        f"Attribute {name!r} is not a usable case-level attribute of "
        f"this log. Detected attributes: {available}"
    )


# ---------------------------------------------------------------------------
# Per-attribute value assignment
# ---------------------------------------------------------------------------

def _fmt_number(x: float) -> str:
    """``40.0 -> "40"``, ``39.5 -> "39.5"`` — compact range labels."""
    xf = float(x)
    return str(int(xf)) if xf == int(xf) else f"{xf:g}"


def _range_value(lo: float, hi: float) -> PartitionValue:
    label = f"{_fmt_number(lo)}-{_fmt_number(hi)}"
    return PartitionValue(
        label=label,
        token=_sanitise_jucmnav_name(label),
        kind="range",
        lo=float(lo),
        hi=float(hi),
    )


def _single_value_bin(v: float) -> PartitionValue:
    """A degenerate ``[v, v]`` bin — one discrete integer value shown as
    itself (``"3"``) rather than a ``"3-3"`` range."""
    label = _fmt_number(v)
    return PartitionValue(
        label=label,
        token=_sanitise_jucmnav_name(label),
        kind="range",
        lo=float(v),
        hi=float(v),
    )


def _discrete_integer_values(numeric, bins: int):
    """The sorted distinct values if ``numeric`` is a small set of whole
    numbers worth binning one-per-value, else ``None``.

    Triggers when the column holds at most ``bins`` distinct integral
    values (e.g. priority levels 1..5 with 5 bins requested): quantile
    bins would merge or split them into ranges, so a reader loses the
    original levels. Requires as many requested bins as distinct values,
    so a smaller bin count still falls through to quantile ranges."""
    import numpy as np

    uniq = np.unique(numeric.to_numpy())
    if uniq.size == 0 or uniq.size > max(1, bins):
        return None
    if not bool(np.all(uniq == np.round(uniq))):
        return None
    return [float(v) for v in uniq]


def _assign_enumeration(
    series,
    spec: AttributeSpec,
    max_values: int,
    other_bucket: bool,
    ignore_case: bool = True,
):
    """Return ``(values_axis, {case_id: PartitionValue or None})`` for an
    enumeration attribute. ``None`` marks a dropped case (trimmed value
    with ``other_bucket=False``).

    With ``ignore_case`` (the default), raw values that differ only in
    letter case (``"F"`` / ``"f"``) are **one** partition value: the
    displayed label is the log's most frequent spelling (ties broken
    alphabetically), and every merged spelling is recorded in
    :attr:`PartitionValue.raw_values`."""
    non_null = series.dropna()
    counts = non_null.astype(str).value_counts()

    # Group raw spellings that denote the same value. ``forms_for``
    # maps the canonical (displayed) spelling to every raw form it
    # absorbs; ``value_counts`` are the merged per-value counts.
    forms_for: Dict[str, List[str]] = {}
    value_counts: Dict[str, int] = {}
    if ignore_case:
        by_key: Dict[str, List[str]] = {}
        for raw in counts.index:
            by_key.setdefault(str(raw).casefold(), []).append(str(raw))
        for forms in by_key.values():
            canon = sorted(
                forms, key=lambda f: (-int(counts[f]), f),
            )[0]
            forms_for[canon] = sorted(forms)
            value_counts[canon] = sum(int(counts[f]) for f in forms)
    else:
        for raw in counts.index:
            forms_for[str(raw)] = [str(raw)]
            value_counts[str(raw)] = int(counts[raw])

    raw_sorted = sorted(value_counts)

    kept = raw_sorted
    merged: List[str] = []
    if max_values and len(raw_sorted) > max_values:
        # Keep the (max_values - 1) most frequent values — determinism
        # via the (count desc, value asc) tie-break — and merge the
        # rest into ``Other``.
        by_freq = sorted(
            raw_sorted, key=lambda v: (-value_counts[v], v),
        )
        kept = sorted(by_freq[: max_values - 1])
        merged = sorted(set(raw_sorted) - set(kept))

    value_for_raw: Dict[str, Optional[PartitionValue]] = {}
    axis: List[PartitionValue] = []
    for value in kept:
        pv = PartitionValue(
            label=value,
            token=spec.sanitise_value(value),
            kind="value",
            raw_values=tuple(forms_for[value]),
        )
        axis.append(pv)
        for form in forms_for[value]:
            value_for_raw[form] = pv
    if merged:
        merged_forms = [f for value in merged for f in forms_for[value]]
        other = (
            PartitionValue(
                label=OTHER_LABEL,
                token=_sanitise_jucmnav_name(OTHER_LABEL),
                kind="other",
                raw_values=tuple(sorted(merged_forms)),
            )
            if other_bucket else None
        )
        if other is not None:
            axis.append(other)
        for form in merged_forms:
            value_for_raw[form] = other

    import pandas as pd

    assignment: Dict[str, Optional[PartitionValue]] = {}
    for cid, v in series.items():
        assignment[cid] = (
            None if pd.isna(v) else value_for_raw.get(str(v))
        )
    return axis, assignment


def _assign_boolean(series, spec: AttributeSpec):
    """``(values_axis, {case_id: PartitionValue or None})`` for booleans."""
    true_pv = PartitionValue(label="true", token="true", kind="value",
                             raw_values=("true",))
    false_pv = PartitionValue(label="false", token="false", kind="value",
                              raw_values=("false",))

    def classify(v):
        if v is True or v == 1 or str(v).casefold() == "true":
            return true_pv
        if v is False or v == 0 or str(v).casefold() == "false":
            return false_pv
        return None

    assignment = {cid: classify(v) for cid, v in series.dropna().items()}
    axis = [pv for pv in (true_pv, false_pv)
            if any(a is pv for a in assignment.values())]
    # Cases whose value didn't classify (or was NaN) are handled by the
    # caller's unknown-bucket pass — report them as missing.
    full = {cid: assignment.get(cid) for cid in series.index}
    return axis, full


def _assign_integer(
    series,
    spec: AttributeSpec,
    bins: int,
    edges: Optional[Sequence[float]],
):
    """``(values_axis, {case_id: PartitionValue or None})`` for numerics,
    partitioned into ranges on explicit ``edges`` or on quantiles."""
    import pandas as pd

    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if numeric.empty:
        return [], {cid: None for cid in series.index}

    discrete = None if edges is not None else _discrete_integer_values(
        numeric, bins)
    if discrete is not None:
        # One bin per distinct whole-number value.
        axis = [_single_value_bin(v) for v in discrete]
    else:
        if edges is not None:
            cut_edges = [float(e) for e in edges]
        else:
            # Quantile bins; duplicate edges collapse for skewed data.
            try:
                _, cut_edges = pd.qcut(
                    numeric, q=max(1, bins), retbins=True, duplicates="drop",
                )
                cut_edges = [float(e) for e in cut_edges]
            except (ValueError, IndexError):
                cut_edges = [float(numeric.min()), float(numeric.max())]
        if len(cut_edges) < 2:
            cut_edges = [float(numeric.min()), float(numeric.max()) + 1.0]

        axis = [
            _range_value(cut_edges[i], cut_edges[i + 1])
            for i in range(len(cut_edges) - 1)
        ]

    def classify(x) -> Optional[PartitionValue]:
        # Bins are [lo, next.lo) with the last bin closed on both ends.
        # Out-of-range values (possible with explicit edges) fall to
        # None and end up in the Unknown bucket.
        if x < axis[0].lo or x > axis[-1].hi:
            return None
        for i, pv in enumerate(axis):
            if i == len(axis) - 1:
                if pv.lo <= x <= pv.hi:
                    return pv
            elif pv.lo <= x < axis[i + 1].lo:
                return pv
        return None

    assignment: Dict[str, Optional[PartitionValue]] = {}
    for cid in series.index:
        v = numeric.get(cid)
        assignment[cid] = classify(float(v)) if v is not None and v == v else None
    used = [pv for pv in axis if any(a is pv for a in assignment.values())]
    return used, assignment


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _values_filter_for(
    include_values: Dict[str, Sequence[str]], spec: AttributeSpec,
) -> Optional[Sequence[str]]:
    """Find the ``include_values`` entry for ``spec`` — accepts the
    source column name, the display name, or the jUCMNav name."""
    display = (
        spec.source_name[len("case:"):]
        if spec.source_name.startswith("case:") else spec.source_name
    )
    for key in (spec.source_name, display, spec.jucmnav_name):
        if key in include_values:
            return include_values[key]
    return None


def partition_log(
    log_df,
    attributes: Sequence[str],
    *,
    case_id_col: str = "case:concept:name",
    min_cases: int = 0,
    max_values_per_attribute: int = 12,
    bins: int = 4,
    bin_edges: Optional[Dict[str, Sequence[float]]] = None,
    other_bucket: bool = True,
    unknown_bucket: bool = True,
    max_enum_cardinality: int = 40,
    numeric_coercion_threshold: float = DEFAULT_NUMERIC_COERCION_THRESHOLD,
    include_values: Optional[Dict[str, Sequence[str]]] = None,
    ignore_value_case: bool = True,
) -> Partition:
    """Partition ``log_df`` by the values of 1–2 case-level attributes.

    Parameters
    ----------
    log_df
        Event log as a pandas DataFrame (one row per event).
    attributes
        One or two attribute names — source column names
        (``"case:cancer_type"``) or their sanitised jUCMNav forms.
    case_id_col
        Case identifier column (default ``"case:concept:name"``).
    min_cases
        Observed value combinations with fewer cases are excluded from
        ``cells`` and recorded in ``skipped_cells`` instead.
    max_values_per_attribute
        Cardinality cap per axis: when an enumeration attribute has
        more distinct values, the lowest-count ones are merged into an
        ``Other`` bucket (``other_bucket=True``) or their cases dropped
        (``other_bucket=False``).
    bins / bin_edges
        Integer attributes are partitioned into ranges: ``bins``
        quantile bins by default, or the explicit ascending edge list
        from ``bin_edges[attribute_name]`` (n+1 edges → n ranges).
        Note that a column of numbers serialised as strings is an
        integer axis, not an enumeration one, so it is binned into
        ranges — pass ``numeric_coercion_threshold`` above ``1.0`` to
        keep such a column categorical.
    numeric_coercion_threshold
        Share of a string column's non-null values that must parse as
        numbers for it to be typed as a numeric attribute. See
        :func:`~pm4py_ucm.algo.discovery.scenarios.decision_mining.extract_case_features`.
    unknown_bucket
        Cases with a missing attribute value go to an ``Unknown``
        bucket when ``True``; otherwise they are dropped (counted in
        ``dropped_cases``).
    include_values
        Optional per-attribute value filter:
        ``{attribute_name: [labels]}``. Only the listed axis values
        are kept (labels as they appear on the axis — raw values for
        enumerations, ``"true"``/``"false"``, range labels like
        ``"18-39"``, and the synthetic ``"Other"``/``"Unknown"``);
        cases carrying other values are dropped (counted in
        ``dropped_cases``). Filtering an attribute down to zero
        values raises :class:`ValueError`. With ``ignore_value_case``
        the labels are matched case-insensitively too.
    ignore_value_case
        ``True`` (default): enumeration values that differ only in
        letter case (``"F"`` / ``"f"``) are **one** partition value —
        displayed as the log's most frequent spelling, with every
        merged spelling kept in ``PartitionValue.raw_values``.
        Booleans always classify case-insensitively
        (``TRUE``/``True``/``true``). Pass ``False`` for logs whose
        codes are genuinely case-significant.

    Returns
    -------
    Partition
    """
    if not 1 <= len(attributes) <= 2:
        raise ValueError(
            f"partition_log expects 1 or 2 attributes, got {len(attributes)}"
        )
    if case_id_col not in log_df.columns:
        raise ValueError(f"case id column {case_id_col!r} not in log")

    specs, per_case_raw = detect_case_attributes(
        log_df, case_id_col=case_id_col,
        max_enum_cardinality=max_enum_cardinality,
        numeric_coercion_threshold=numeric_coercion_threshold,
    )
    if not specs:
        raise ValueError(
            "No usable case-level attributes detected in this log — "
            "nothing to partition on."
        )

    bin_edges = bin_edges or {}
    part_attrs: List[PartitionAttribute] = []
    assignments: List[Dict[str, Optional[PartitionValue]]] = []

    for name in attributes:
        spec = _resolve_spec(name, specs)
        series = per_case_raw[spec.source_name]
        if spec.type == "enumeration":
            axis, assignment = _assign_enumeration(
                series, spec, max_values_per_attribute, other_bucket,
                ignore_case=ignore_value_case,
            )
            binned = False
        elif spec.type == "boolean":
            axis, assignment = _assign_boolean(series, spec)
            binned = False
        else:  # integer (possibly scaled float)
            edges = (
                bin_edges.get(spec.source_name)
                or bin_edges.get(spec.jucmnav_name)
            )
            axis, assignment = _assign_integer(series, spec, bins, edges)
            binned = True

        # Missing / unclassifiable values → Unknown bucket (or drop).
        missing = [cid for cid, pv in assignment.items() if pv is None]
        if missing and unknown_bucket:
            unknown = PartitionValue(
                label=UNKNOWN_LABEL,
                token=_sanitise_jucmnav_name(UNKNOWN_LABEL),
                kind="unknown",
            )
            axis = list(axis) + [unknown]
            for cid in missing:
                assignment[cid] = unknown

        # Value filter: keep only the requested axis values; cases
        # carrying anything else are excluded (≠ missing).
        selected = (
            _values_filter_for(include_values, spec)
            if include_values else None
        )
        if selected is not None:
            # Case-insensitive matching mirrors the value merging —
            # a filter of ["f"] must keep the axis value labelled "F".
            if ignore_value_case:
                selected_set = {str(s).casefold() for s in selected}
                kept_axis = [
                    v for v in axis if v.label.casefold() in selected_set
                ]
            else:
                selected_set = {str(s) for s in selected}
                kept_axis = [v for v in axis if v.label in selected_set]
            if not kept_axis:
                raise ValueError(
                    f"include_values for {spec.source_name!r} removes "
                    f"every value; available: {[v.label for v in axis]}"
                )
            keep_ids = {id(v) for v in kept_axis}
            for cid, pv in assignment.items():
                if pv is not None and id(pv) not in keep_ids:
                    assignment[cid] = _EXCLUDED
            axis = kept_axis

        part_attrs.append(
            PartitionAttribute(spec=spec, values=list(axis), binned=binned)
        )
        assignments.append(assignment)

    # ------------------------------------------------------------------
    # Cross the axes: group cases by their value combination.
    # ------------------------------------------------------------------
    total_cases = len(per_case_raw.index)
    combo_cases: Dict[Tuple[int, ...], List[str]] = {}
    dropped = 0
    axis_index = [
        {id(pv): i for i, pv in enumerate(a.values)} for a in part_attrs
    ]
    for cid in per_case_raw.index:
        key: List[int] = []
        ok = True
        for a_i, assignment in enumerate(assignments):
            pv = assignment.get(cid)
            if pv is None or pv is _EXCLUDED:
                ok = False
                break
            key.append(axis_index[a_i][id(pv)])
        if not ok:
            dropped += 1
            continue
        combo_cases.setdefault(tuple(key), []).append(str(cid))

    # Deterministic cell order: position on axis 1, then axis 2.
    cells: List[PartitionCell] = []
    skipped: List[Tuple[Tuple[PartitionValue, ...], int]] = []
    case_series = log_df[case_id_col].astype(str)
    for key in sorted(combo_cases):
        case_ids = combo_cases[key]
        values = tuple(
            part_attrs[i].values[j] for i, j in enumerate(key)
        )
        if min_cases and len(case_ids) < min_cases:
            skipped.append((values, len(case_ids)))
            continue
        sub_df = log_df[case_series.isin(set(case_ids))]
        cells.append(
            PartitionCell(values=values, case_ids=case_ids, df=sub_df)
        )

    # Trim axes down to values that actually survived into a cell or a
    # skipped combination — an Unknown/Other bucket with zero cases
    # must not become a grid row.
    seen_value_ids = set()
    for c in cells:
        seen_value_ids.update(id(v) for v in c.values)
    for values, _ in skipped:
        seen_value_ids.update(id(v) for v in values)
    for a in part_attrs:
        a.values = [v for v in a.values if id(v) in seen_value_ids]

    return Partition(
        attributes=part_attrs,
        cells=cells,
        skipped_cells=skipped,
        total_cases=total_cases,
        dropped_cases=dropped,
        case_id_col=case_id_col,
    )
