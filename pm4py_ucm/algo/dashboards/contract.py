"""The per-case *fact table* — the data contract between the Python
backend and the client-side metrics engine.

Dashboards are computed in the browser: filtering, segmentation,
aggregation and target scoring all run client-side over a compact
snapshot of the log, so that reader-side filters in the exported HTML
recompute every widget without a server. This module builds that
snapshot and encodes it for transport.

Why a fact table rather than pre-aggregated series
--------------------------------------------------
A widget's value depends on the dashboard filter, the widget filter and
the segmentation axes, all of which the reader can change *after*
export. Pre-aggregating would freeze those choices. Shipping per-case
facts keeps every catalog metric — including ``time_between(a, b)`` for
activity pairs nobody picked at export time — computable in the reader.

Encoding
--------
The obvious encoding (an array of per-case objects with nested event
lists) is both large and slow to build. Measured on the project's own
logs:

===================  ===================  ==========================
log                  array-of-objects     columnar binary + base64
===================  ===================  ==========================
ClaimsPaymentLog     2.17 MB / 4.5 s      0.69 MB / 0.1 s
IssueTracker         2.94 MB / 7.9 s      0.92 MB / 0.1 s
===================  ===================  ==========================

so the payload is **columnar**: activity names and resources are
dictionary-encoded to integer codes, event timestamps are delta-encoded
against their case's start, and the per-case event lists are flattened
into one array addressed by CSR-style offsets. Each buffer is a typed
little-endian array, base64-encoded. The JS side decodes straight into
``TypedArray`` views.

Every buffer declares its own ``dtype`` in the payload, so integer
widths can be promoted (a log with >65 535 distinct activities) without
the decoder caring.

Size
----
The encoding costs ~6 bytes per event plus ~8 bytes per case, before
base64's 4/3 expansion. A 561 k-event log therefore lands near 6 MB —
comparable to the PNGs :mod:`~pm4py_ucm.algo.discovery.families.report`
already embeds, and cheap enough that no sampling is needed for
realistic logs. :func:`build_fact_table` nonetheless enforces a byte
budget and falls back to a deterministic case sample beyond it, so a
pathological log degrades honestly (and says so in
:attr:`FactTable.sampled_from`) rather than hanging the browser.

Time resolution
---------------
Timestamps are stored as epoch **seconds**, rounded to nearest. Real logs
carry sub-second components (ClaimsPaymentLog has milliseconds), so a
duration computed here can differ from the raw log by up to half a second
per endpoint. That is deliberate: it keeps the delta buffers in
``uint32`` (halving the payload against ``float64``), and it is orders of
magnitude below the resolution of any metric in the catalog — durations
are reported in days, to one decimal, where a second is 1/8640th of the
last displayed digit.

Missing values
--------------
Integer-coded columns reserve their dtype's maximum value as the
"missing" sentinel; float columns use ``NaN``. The JS engine treats both
as *no value*, which propagates to ``null`` and is excluded from
aggregation — matching the Python engine's ``dropna`` semantics.

Events whose timestamp cannot be parsed are **dropped**, and the count is
kept in :attr:`FactTable.dropped_events` for the UI to surface. They
cannot be kept: an unparseable timestamp has no position in a case's
event order, and letting its sentinel value reach the delta arithmetic
would silently corrupt that case's start rather than fail.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Contract version embedded in the payload. Bump on any incompatible
#: change to buffer layout or field meaning; the JS decoder refuses a
#: version it does not know.
CONTRACT_VERSION = 1

#: Default ceiling for the encoded payload, in bytes. Beyond this
#: :func:`build_fact_table` samples cases rather than shipping the whole
#: log into a browser tab. 24 MB is well past every log the project
#: ships or has been tested on; it exists to bound the pathological case.
DEFAULT_MAX_PAYLOAD_BYTES = 24_000_000

#: Cases retained when the byte budget forces sampling.
DEFAULT_SAMPLE_CASES = 40_000

#: Seed for the sampling RNG — a given log always yields the same sample,
#: so an export is reproducible and two readers see identical numbers.
SAMPLE_SEED = 42


@dataclass
class NumericBin:
    """One bucket of a binned numeric attribute.

    Bins are ``[lo, hi)`` except the last, which is closed on both ends —
    the same convention as
    :func:`~pm4py_ucm.algo.discovery.families.partition._assign_integer`,
    so a dashboard segmented by a binned attribute agrees with the family
    grid partitioned on the same attribute.
    """

    label: str
    lo: float
    hi: float

    def to_json(self) -> Dict[str, Any]:
        return {"label": self.label, "lo": self.lo, "hi": self.hi}


@dataclass
class CaseAttribute:
    """A case-constant attribute usable as a filter or segmentation axis.

    Mirrors
    :class:`~pm4py_ucm.algo.discovery.scenarios.decision_mining.AttributeSpec`
    but carries only what the client engine needs, plus the quantile bin
    edges for numerics (the client bins, so a reader can be shown the
    boundaries and — later — move them).
    """

    #: Source column in the event log, e.g. ``"case:amount"``.
    name: str
    #: Display name — ``name`` without any ``case:`` prefix.
    label: str
    #: ``"enumeration"``, ``"integer"`` or ``"boolean"``.
    type: str
    #: Dictionary of raw values, for ``"enumeration"`` only. The
    #: per-case buffer holds indices into this list.
    values: List[str] = field(default_factory=list)
    #: Quantile bins, for ``"integer"`` only. The per-case buffer holds
    #: the raw numeric value; the client assigns bins from these edges.
    bins: List[NumericBin] = field(default_factory=list)
    #: Buffer key in :attr:`FactTable.buffers` holding this attribute's
    #: per-case column.
    buffer: str = ""

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "buffer": self.buffer,
        }
        if self.values:
            out["values"] = self.values
        if self.bins:
            out["bins"] = [b.to_json() for b in self.bins]
        return out


@dataclass
class FactTable:
    """A log snapshot the client engine can compute any catalog metric on.

    Timestamps are epoch **seconds** throughout: ``start[i]`` is case
    *i*'s first event, and an event's absolute time is
    ``start[i] + dt[j]``. Second resolution is deliberate — it keeps the
    delta buffers in ``uint32`` and no process-mining metric in the
    catalog is meaningful at finer granularity.
    """

    #: Display name of the source log.
    log_name: str
    #: Activity dictionary; ``act`` buffer holds indices into it.
    activities: List[str]
    #: Resource dictionary (``org:resource``); empty when the log has no
    #: resource column. The ``res`` buffer holds indices into it.
    resources: List[str]
    #: Case-level attributes available as filters/segments.
    attributes: List[CaseAttribute]
    #: ``{key: numpy array}`` — see the module docstring for the layout.
    #: Held as arrays rather than encoded bytes so the Python engine
    #: computes from exactly the representation the JS engine decodes;
    #: parity is then a property of the design, not of two hand-kept
    #: implementations agreeing.
    buffers: Dict[str, Any]
    n_cases: int
    n_events: int
    #: True when the log carries a ``start_timestamp`` column, so the
    #: ``sdt`` buffer is present and service-time metrics are derivable.
    #: Single-timestamp logs get sojourn times only — the same split
    #: :mod:`pm4py_ucm.algo.performance` documents.
    interval_log: bool = False
    #: Population size when the table is a sample, else ``None``. Set
    #: only by the byte-budget fallback; the UI must surface it, since
    #: every number computed from a sampled table is an estimate.
    sampled_from: Optional[int] = None
    #: Events discarded for an unparseable timestamp. Normally 0; when
    #: not, the UI should say so rather than let the reader assume the
    #: dashboard covers the whole log.
    dropped_events: int = 0
    #: The case-id values, in the table's case order (buffer row *i* is
    #: ``case_ids[i]``). Kept **server-side only** — never serialised into
    #: :meth:`to_payload` — so a caller (e.g. a ƒ attribute log-filter) can
    #: map a per-case result array back to case ids without re-deriving the
    #: table's ordering. Empty on a table decoded from a payload.
    case_ids: List[Any] = field(default_factory=list)

    @property
    def sampled(self) -> bool:
        return self.sampled_from is not None

    def to_payload(self) -> Dict[str, Any]:
        """The JSON-ready payload handed to the client engine."""
        return {
            "version": CONTRACT_VERSION,
            "log": {
                "name": self.log_name,
                "n_cases": self.n_cases,
                "n_events": self.n_events,
                "interval": self.interval_log,
                "sampled_from": self.sampled_from,
                "dropped_events": self.dropped_events,
            },
            "activities": self.activities,
            "resources": self.resources,
            "attributes": [a.to_json() for a in self.attributes],
            "buffers": {
                key: {
                    "dtype": arr.dtype.name,
                    "b64": base64.b64encode(
                        arr.astype(arr.dtype.newbyteorder("<"),
                                   copy=False).tobytes()).decode("ascii"),
                }
                for key, arr in self.buffers.items()
            },
        }

    def payload_bytes(self) -> int:
        """Approximate encoded size, without building the payload.

        base64 expands by 4/3; the JSON scaffolding around the buffers is
        negligible next to them.
        """
        raw = sum(a.nbytes for a in self.buffers.values())
        return (raw * 4 + 2) // 3

    # -- accessors the engine reads through ---------------------------

    @property
    def offsets(self):
        """CSR offsets: case *i* owns events ``[off[i], off[i+1])``."""
        return self.buffers["off"]

    @property
    def starts(self):
        """Per-case first-event time, epoch seconds."""
        return self.buffers["start"]

    @property
    def act_codes(self):
        """Per-event activity index into :attr:`activities`."""
        return self.buffers["act"]

    @property
    def dt(self):
        """Per-event seconds since its case's start."""
        return self.buffers["dt"]

    def attribute(self, name: str) -> Optional[CaseAttribute]:
        """Look up a case attribute by source name or display label."""
        for a in self.attributes:
            if name in (a.name, a.label):
                return a
        return None


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def _uint_dtype(n_values: int) -> str:
    """Narrowest unsigned dtype holding ``0..n_values`` *and* a distinct
    maximum-value sentinel for "missing"."""
    import numpy as np

    for width, name in ((8, "uint8"), (16, "uint16"), (32, "uint32")):
        if n_values < (1 << width) - 1:  # room left for the sentinel
            return name
    return "uint64"


def _sentinel(dtype: str) -> int:
    import numpy as np

    return int(np.iinfo(dtype).max)


def _code_column(values, dictionary: Sequence[str]):
    """Dictionary-encode ``values`` into a typed array, mapping anything
    outside ``dictionary`` (including NaN) to the missing sentinel."""
    import numpy as np
    import pandas as pd

    dtype = _uint_dtype(len(dictionary))
    lookup = {v: i for i, v in enumerate(dictionary)}
    codes = pd.Series(values).reset_index(drop=True).map(lookup)
    return codes.fillna(_sentinel(dtype)).to_numpy(dtype=dtype)


#: Integer ticks per second for each datetime64 resolution pandas may
#: hand us. The resolution is **not** fixed: ``pm4py.read_xes`` yields
#: nanoseconds, while pandas 3 infers microseconds for constructed
#: timestamps and a CSV parse can yield either. Assuming one unit silently
#: scales every duration by a power of ten, so the unit is always read
#: from the dtype rather than presumed.
_TICKS_PER_SECOND = {"s": 1, "ms": 10**3, "us": 10**6, "ns": 10**9}


def _epoch_seconds(series):
    """``(seconds, valid)`` — epoch seconds rounded to nearest, plus a
    mask of the entries that parsed.

    Rounds rather than truncates: flooring both endpoints of a duration
    biases it, and rounding halves the worst-case error to 0.5 s per
    endpoint. See the module docstring on time resolution.
    """
    import numpy as np
    import pandas as pd

    ts = pd.to_datetime(series, utc=True, errors="coerce")
    valid = ts.notna().to_numpy()

    try:
        unit = ts.dt.unit
    except AttributeError:  # very old pandas: datetime64[ns] was the only one
        unit = "ns"
    per_sec = _TICKS_PER_SECOND.get(unit)
    if per_sec is None:
        raise ValueError(
            f"Unsupported timestamp resolution {unit!r}; expected one of "
            f"{sorted(_TICKS_PER_SECOND)}."
        )

    ticks = ts.astype("int64").to_numpy()
    secs = np.zeros(ticks.shape, dtype=np.int64)
    # Round only where valid — NaT's int64 sentinel must never reach the
    # arithmetic, where it would sentinel a case's start to nonsense.
    if valid.any():
        secs[valid] = (ticks[valid] + per_sec // 2) // per_sec
    return secs, valid


#: A whole-number case attribute with fewer than this many distinct values is
#: offered as its **discrete levels** — one bin per value, which the filter and
#: segment UIs present as individual selectable values (like an enumeration) —
#: rather than quantile ranges. So a 1..5 rating reads and segments as its five
#: levels, not "1–2 / 2–3 / 4–5", *regardless of the requested bin count*. At or
#: above it the column is treated as genuinely continuous and gets quantile
#: bins. The attribute stays ``type="integer"`` (its per-case buffer keeps the
#: raw number, so the ƒ-formula ``attr(...)`` still reads a value to compare).
_MAX_DISCRETE_LEVELS = 10


def _fmt_num(x: float) -> str:
    """Format a numeric level: a plain integer when whole, else 4 sig figs."""
    return f"{x:.0f}" if float(x).is_integer() else f"{x:.4g}"


def _numeric_bins(numeric, bins: int) -> List[NumericBin]:
    """Quantile bins over a numeric case column.

    Uses the same ``qcut``-with-``duplicates="drop"`` approach as the
    family partitioner, so skewed columns collapse duplicate edges
    instead of raising.
    """
    import numpy as np
    import pandas as pd

    clean = pd.to_numeric(numeric, errors="coerce").dropna()
    if clean.empty:
        return []

    # Few distinct whole-number values (e.g. a 1..5 rating): one bin per value,
    # so a reader sees "3" rather than a "2–3" range that merges or splits the
    # levels — and the filter / segment UIs offer the individual levels like an
    # enumeration. This wins over the requested bin count: a low-cardinality
    # level column is discrete, not something to quantile. Degenerate [v, v]
    # bins segment correctly — both engines threshold on the next bin's ``lo``,
    # so the last bin absorbs the maximum.
    uniq = np.unique(clean.to_numpy())
    if 0 < uniq.size < _MAX_DISCRETE_LEVELS and bool(
            np.all(uniq == np.round(uniq))):
        return [NumericBin(label=_fmt_num(float(v)), lo=float(v), hi=float(v))
                for v in uniq]

    try:
        _, edges = pd.qcut(clean, q=max(1, bins), retbins=True,
                           duplicates="drop")
        edges = [float(e) for e in edges]
    except (ValueError, IndexError):
        edges = [float(clean.min()), float(clean.max())]
    if len(edges) < 2:
        edges = [float(clean.min()), float(clean.max()) + 1.0]

    return [
        NumericBin(label=f"{_fmt_num(edges[i])}–{_fmt_num(edges[i + 1])}",
                   lo=edges[i], hi=edges[i + 1])
        for i in range(len(edges) - 1)
    ]


def _build_attributes(log_df, case_ids, case_id_col: str,
                      bins: int) -> Tuple[List[CaseAttribute],
                                          Dict[str, Any]]:
    """Case-level attribute columns, aligned to ``case_ids`` order.

    Reuses the family partitioner's detection so a dashboard offers
    exactly the attributes the Family view can partition on — same
    typing, same boolean/integer/enumeration classification.
    """
    import warnings

    import numpy as np
    import pandas as pd

    from ..discovery.families import detect_case_attributes

    try:
        with warnings.catch_warnings():
            # A log with no case-constant attributes is perfectly normal
            # for a dashboard — it simply offers time and resource axes
            # only. The detector warns about abandoning OR-fork mining,
            # which is not what is being attempted here and would only
            # confuse.
            warnings.filterwarnings(
                "ignore", message=".*case-constant attributes.*")
            specs, per_case_raw = detect_case_attributes(
                log_df, case_id_col=case_id_col)
    except Exception:  # detection is best-effort; a log may have none
        return [], {}
    if not specs or per_case_raw is None:
        return [], {}

    # per_case_raw is indexed by case ID; reindex onto our case order so
    # attribute row i describes case i in every other buffer.
    aligned = per_case_raw.reindex(case_ids)

    attrs: List[CaseAttribute] = []
    buffers: Dict[str, Any] = {}
    for i, spec in enumerate(sorted(specs.values(),
                                    key=lambda s: s.source_name)):
        col = spec.source_name
        if col not in aligned.columns:
            continue
        series = aligned[col]
        key = f"attr{i}"
        label = col[len("case:"):] if col.startswith("case:") else col

        if spec.type == "integer":
            buffers[key] = pd.to_numeric(
                series, errors="coerce").to_numpy(dtype=np.float64)
            attrs.append(CaseAttribute(
                name=col, label=label, type="integer",
                bins=_numeric_bins(series, bins), buffer=key))
        else:
            # Boolean and enumeration share an encoding; the type is kept
            # so the UI can offer a two-state toggle rather than a
            # multiselect. Labels are the log's own vocabulary, not a
            # normalised true/false.
            vals = [str(v) for v in
                    sorted(series.dropna().astype(str).unique())]
            buffers[key] = _code_column(
                series.astype(str).where(series.notna()), vals)
            attrs.append(CaseAttribute(
                name=col, label=label, type=spec.type,
                values=vals, buffer=key))
    return attrs, buffers


def build_fact_table(
    log_df,
    *,
    log_name: str = "log",
    case_id_col: str = "case:concept:name",
    activity_col: str = "concept:name",
    timestamp_col: str = "time:timestamp",
    start_timestamp_col: str = "start_timestamp",
    resource_col: str = "org:resource",
    bins: int = 4,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    sample_cases: int = DEFAULT_SAMPLE_CASES,
) -> FactTable:
    """Build the client-side fact table for ``log_df``.

    Parameters
    ----------
    log_df
        The event log as a DataFrame — the same object the family
        partitioner works on (``_load_log_df`` in the web app).
    bins
        Quantile bins per numeric case attribute.
    max_payload_bytes
        Byte budget for the encoded payload. When the full table would
        exceed it, a deterministic sample of ``sample_cases`` cases is
        built instead and :attr:`FactTable.sampled_from` records the true
        population. Set to ``0`` to disable the budget entirely.

    Returns
    -------
    FactTable
        Ready to :meth:`~FactTable.to_payload`.
    """
    import numpy as np
    import pandas as pd

    for col in (case_id_col, activity_col, timestamp_col):
        if col not in log_df.columns:
            raise ValueError(
                f"Log is missing the required column {col!r}. "
                f"Available columns: {sorted(log_df.columns)}"
            )

    full_cases = int(log_df[case_id_col].nunique())
    sampled_from: Optional[int] = None

    # A cheap up-front estimate (6 B/event + 8 B/case, ×4/3 for base64)
    # decides sampling without building the buffers twice.
    if max_payload_bytes:
        est = ((len(log_df) * 6 + full_cases * 8) * 4) // 3
        if est > max_payload_bytes and full_cases > sample_cases:
            rng = np.random.default_rng(SAMPLE_SEED)
            uniq = pd.Index(log_df[case_id_col].unique())
            keep = set(uniq[rng.choice(len(uniq), size=sample_cases,
                                       replace=False)])
            log_df = log_df[log_df[case_id_col].isin(keep)]
            sampled_from = full_cases

    # Drop unparseable timestamps before anything positional depends on
    # them; see the module docstring.
    _, ts_valid = _epoch_seconds(log_df[timestamp_col])
    dropped_events = int((~ts_valid).sum())
    if dropped_events:
        log_df = log_df[ts_valid]

    df = log_df.sort_values([case_id_col, timestamp_col], kind="stable")
    ts, _ = _epoch_seconds(df[timestamp_col])

    case_codes, case_ids = pd.factorize(df[case_id_col], sort=False)
    n_cases = len(case_ids)
    counts = np.bincount(case_codes, minlength=n_cases)
    offsets = np.zeros(n_cases + 1, dtype=np.uint32)
    np.cumsum(counts, out=offsets[1:])

    starts = np.minimum.reduceat(ts, offsets[:-1].astype(np.int64))
    deltas = (ts - np.repeat(starts, counts))
    # A clock skew or unparsed timestamp must not wrap the unsigned
    # buffer into a colossal duration; clamp and let it read as 0.
    deltas = np.clip(deltas, 0, None).astype(np.uint32)

    activities = sorted(df[activity_col].astype(str).unique())

    buffers: Dict[str, Any] = {
        "off": offsets,
        "start": starts.astype(np.uint32),
        "act": _code_column(df[activity_col].astype(str), activities),
        "dt": deltas,
    }

    resources: List[str] = []
    if resource_col in df.columns:
        resources = sorted(df[resource_col].dropna().astype(str).unique())
        if resources:
            buffers["res"] = _code_column(
                df[resource_col].astype(str).where(df[resource_col].notna()),
                resources)

    interval = start_timestamp_col in df.columns
    if interval:
        sts, sts_valid = _epoch_seconds(df[start_timestamp_col])
        # A start after its own completion is meaningless — mark it
        # missing rather than clamp it to a fake zero-length service time
        # that would quietly drag every mean down. Same for a start that
        # did not parse, and for one before its own case's start.
        bad = ~sts_valid | (sts > ts) | (sts < np.repeat(starts, counts))
        sdt = np.clip(sts - np.repeat(starts, counts), 0, None).astype(
            np.uint32)
        sdt[bad] = np.iinfo(np.uint32).max
        buffers["sdt"] = sdt

    attrs, attr_buffers = _build_attributes(
        log_df, pd.Index(case_ids), case_id_col, bins)
    buffers.update(attr_buffers)

    return FactTable(
        log_name=log_name,
        activities=activities,
        resources=resources,
        attributes=attrs,
        buffers=buffers,
        n_cases=n_cases,
        n_events=int(len(df)),
        interval_log=interval,
        sampled_from=sampled_from,
        dropped_events=dropped_events,
        case_ids=list(case_ids),
    )
