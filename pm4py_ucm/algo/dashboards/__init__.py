"""User-defined dashboards over an event log.

A dashboard is an ordered list of *widget specs* plus a dashboard-level
filter. A widget spec — the core artifact, serialised as JSON — names a
catalog metric, its parameters, an aggregation, a filter, segmentation
axes, a visualisation and an optional target::

    {
      "id": "w1", "title": "avg Send Fine → Payment",
      "metric": "timeBetween",
      "params": {"from": "Send Fine", "to": "Payment"},
      "agg": "avg",
      "filter": [{"field": "attr:amount", "op": ">", "value": 100}],
      "segment": {"rows": "resource", "cols": "quarter"},
      "viz": "table",
      "target": {"on": true, "dir": "<=", "value": 14, "warn": 18,
                 "mode": "aggregate"}
    }

Modules:

* :mod:`.contract` — the per-case fact table shipped to the client;
* :mod:`.catalog` — what a widget can measure;
* :mod:`.engine` — per-case values, filters, segmentation, aggregation,
  targets and widget computation;
* :mod:`.view` — assembles the self-contained HTML artifact that serves
  as *both* the app's Dashboards view and the exported report.

The engine exists twice: here, and in JS (``web/assets/dash-engine.js``)
for the browser and the self-contained HTML export. Both read the same
:mod:`.contract` payload and return the same widget structure. The pair
is pinned by a parity test rather than by good intentions — see
``tests/test_dashboards.py``.
"""

from .contract import (  # noqa: F401
    CONTRACT_VERSION,
    CaseAttribute,
    FactTable,
    NumericBin,
    build_fact_table,
)
from .catalog import (  # noqa: F401
    CATALOG,
    BY_ID,
    MetricSpec,
    Param,
    catalog_json,
)
from .view import (  # noqa: F401
    bundle_script,
    dashboard_html,
    write_dashboard,
)
from .formula import (  # noqa: F401
    FUNCTIONS,
    FormulaError,
    compile_formula,
    parse as parse_formula,
    result_type,
)
from .engine import (  # noqa: F401
    AGGS,
    STATE_UI,
    aggregate,
    apply_filters,
    compute_widget,
    fmt,
    per_case_values,
    scorecard,
    segment_axes,
    segment_keys,
    series_values,
    target_state,
    worst_state,
)
