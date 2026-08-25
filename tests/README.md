# Test suite

**1107 tests** across **35 modules**, grouped by area below. Counts are pytest-collected items (parametrised cases counted individually), so they match `pytest` exactly.

> Regenerate this file with `python tests/gen_readme.py` after adding or removing tests. Purposes are the first sentence of each module's docstring; edit the docstring, not this table.

| Area | Module | Purpose | Tests |
| --- | --- | --- | ---: |
| **Object model & jUCMNav I/O** | [`test_export_import.py`](test_export_import.py) | Tests for the jUCMNav-compatible XMI exporter and importer. | 34 |
|  | [`test_name_wrap.py`](test_name_wrap.py) | Tests for the multi-line name-wrapping helper and its integration with the exporter (issue 2 of the visual-improvements series). | 10 |
|  | [`test_obj.py`](test_obj.py) | Unit tests for the UCM object model. | 7 |
|  | [`test_stub_bindings.py`](test_stub_bindings.py) | Tests for stub plug-in binding round-trip. | 15 |
|  | [`test_validate.py`](test_validate.py) | Structural well-formedness of a UCM. | 16 |
| | _Object model & jUCMNav I/O subtotal_ | | **82** |
| **Discovery & conversion** | [`test_boolean_detection.py`](test_boolean_detection.py) | Case-insensitive boolean type detection in decision mining (issue #6). | 12 |
|  | [`test_choice_signature.py`](test_choice_signature.py) | Smoke tests for the concurrency-aware choice signature. | 20 |
|  | [`test_conversion.py`](test_conversion.py) | Tests for the process tree → UCM converter. | 7 |
|  | [`test_decomposition.py`](test_decomposition.py) | Tests for hierarchical decomposition of process trees into multi-map UCMs. | 39 |
|  | [`test_expression_minimizer.py`](test_expression_minimizer.py) | Unit tests for the boolean simplifier used on data-driven OR-fork conditions. | 17 |
|  | [`test_numeric_string_typing.py`](test_numeric_string_typing.py) | Numeric attributes serialised as strings must type as integers. | 25 |
| | _Discovery & conversion subtotal_ | | **120** |
| **Resources & performers** | [`test_resources.py`](test_resources.py) | Tests for resource mining and performer binding. | 22 |
| **Scenario synthesis** | [`test_scenario_synthesis.py`](test_scenario_synthesis.py) | End-to-end smoke tests for variant clustering + scenario synthesis. | 59 |
| **Scenario simulation & coverage** | [`test_scenario_coverage.py`](test_scenario_coverage.py) | Coverage over a set of scenarios, and the A/B comparison. | 17 |
|  | [`test_scenario_traversal.py`](test_scenario_traversal.py) | Offline simulation of jUCMNav's scenario traversal. | 40 |
| | _Scenario simulation & coverage subtotal_ | | **57** |
| **Model families** | [`test_family.py`](test_family.py) | Tests for attribute-based model families. | 49 |
|  | [`test_family_stats.py`](test_family_stats.py) | Tests for the family statistics layer and the HTML report. | 32 |
|  | [`test_partition_advisor.py`](test_partition_advisor.py) | Tests for the deterministic partition advisor (docs/ai_insights.md §4.1b). | 8 |
| | _Model families subtotal_ | | **89** |
| **Performance & metrics** | [`test_metric_validation.py`](test_metric_validation.py) | Metric-correctness validation suite. | 45 |
|  | [`test_performance.py`](test_performance.py) | Tests for the performance overlay (frequencies / times on activities and edges): stats computation, segment-based annotation, visualizer output, and .jucm round-trip of the metadata. | 17 |
|  | [`test_traversal.py`](test_traversal.py) | Tests for replay-based traversal counting. | 24 |
| | _Performance & metrics subtotal_ | | **86** |
| **Cost screening & log reduction** | [`test_complexity.py`](test_complexity.py) | Tests for the discovery/replay cost screening in ``algo.complexity``. | 12 |
|  | [`test_quick_reductions.py`](test_quick_reductions.py) | V6's one-click reductions record what they selected, and hold. | 12 |
|  | [`test_replay_budget.py`](test_replay_budget.py) | Tests for telling a truncated replay apart from a trace that cannot fit. | 7 |
| | _Cost screening & log reduction subtotal_ | | **31** |
| **Layout** | [`test_graphviz_layout.py`](test_graphviz_layout.py) | Tests for the graphviz-based layouter. | 8 |
|  | [`test_layout.py`](test_layout.py) | Tests for the compact left-to-right auto-layouter. | 15 |
|  | [`test_routing_points.py`](test_routing_points.py) | Tests for the routing-empty-points pass (issue 4). | 7 |
| | _Layout subtotal_ | | **30** |
| **Visualization** | [`test_heatmap.py`](test_heatmap.py) | Tests for the performance heat-map (render-time emphasis) in `pm4py_ucm.visualization.ucm.variants.classic`. | 19 |
|  | [`test_ucm_svg.py`](test_ucm_svg.py) | Tests for inline-SVG rendering of UCM models (`pm4py_ucm.visualization.ucm.svg`). | 13 |
|  | [`test_visualization.py`](test_visualization.py) | Tests for the graphviz-based UCM visualiser. | 35 |
| | _Visualization subtotal_ | | **67** |
| **Dashboards** | [`test_dashboards.py`](test_dashboards.py) | Tests for user-defined dashboards: the client contract, the metric engine, and the parity of the Python engine with its JS counterpart. | 399 |
| **Sessions (save / share / resume)** | [`test_sessions.py`](test_sessions.py) | Tests for the project save/share/resume core (``web/sessions``). | 14 |
|  | [`test_sessions_codegen.py`](test_sessions_codegen.py) | Tests for the Python code exporter (``web/sessions/codegen.py``). | 26 |
|  | [`test_sessions_registry.py`](test_sessions_registry.py) | Tests for the Session Parameter Registry (``web/sessions/registry.py``). | 10 |
| | _Sessions (save / share / resume) subtotal_ | | **50** |
| **Progress / infrastructure** | [`test_progress.py`](test_progress.py) | Tests for the progress-reporting layer and the vectorized DataFrame resource miner. | 15 |
| | **Total** | | **1107** |

See the repository [`README.md`](../README.md#testing) for how to run the suite, and [`docs/metrics.md`](../docs/metrics.md) for the metric definitions the validation tests enforce.
