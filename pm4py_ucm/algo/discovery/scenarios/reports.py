"""CSV reports for variant clustering and scenario synthesis.

Two complementary outputs:

* :func:`write_variants_report` — one row per variant, with
  partial-order expression, frequency, linearization count, and
  truncated case-ID list. Headline artifact for the empirical section
  of a paper.

* :func:`write_case_variant_map` — one row per case, mapping case ID
  to its variant ID (or ``"noise"`` for non-conforming cases). Joins
  cleanly against the user's own log to enrich downstream analysis.

Both writers accept either a string path or any writable text
file-like object."""
from __future__ import annotations

import csv
import io
from typing import IO, List, Optional, Sequence, Union

from ..variants.clustering import ClusteringResult, Variant


def write_variants_report(
    clustering: ClusteringResult,
    file_path: Union[str, IO],
    max_case_ids: int = 5,
) -> None:
    """Write a one-row-per-variant CSV summarising ``clustering``.

    Columns:

    1. ``variant_id`` — ``v1``, ``v2``, … (descending frequency).
    2. ``frequency`` — number of cases in this variant.
    3. ``sequence_variants`` — distinct activity sequences observed in
       the log for this variant. ``frequency / sequence_variants`` is
       a quick read of variant heterogeneity.
    4. ``linearization_count`` — the number of total orders the
       variant's partial order admits (i.e. how many sequence-variants
       the model says could fit). ``sequence_variants /
       linearization_count`` measures coverage of the variant's full
       behaviour space.
    5. ``partial_order_expression`` — compact textual rendering.
    6. ``sample_case_ids`` — first ``max_case_ids`` case IDs, then
       ``+N more`` if truncated.

    A trailing row labelled ``noise`` summarises non-conforming
    cases. A second trailing row labelled ``totals`` carries the
    fitness percentage and the compression ratio.
    """
    fields = [
        "variant_id", "frequency", "sequence_variants",
        "linearization_count", "partial_order_expression",
        "sample_case_ids",
    ]

    def _emit(writer):
        writer.writerow(fields)
        for v in clustering.variants:
            sample = v.case_ids[:max_case_ids]
            extra = len(v.case_ids) - len(sample)
            sample_str = ", ".join(sample)
            if extra > 0:
                sample_str += f", +{extra} more"
            writer.writerow([
                v.variant_id,
                v.frequency,
                v.sequence_variants,
                v.linearization_count,
                v.partial_order_expression,
                sample_str,
            ])
        # Noise bucket
        if clustering.noise_case_ids:
            noise_sample = clustering.noise_case_ids[:max_case_ids]
            extra = len(clustering.noise_case_ids) - len(noise_sample)
            noise_str = ", ".join(noise_sample)
            if extra > 0:
                noise_str += f", +{extra} more"
            writer.writerow([
                "noise",
                len(clustering.noise_case_ids),
                "",
                "",
                "(traces did not replay on the tree)",
                noise_str,
            ])
        # Summary row
        writer.writerow([
            "totals",
            clustering.total_cases,
            clustering.sequence_variant_count,
            "",
            (
                f"fitness={clustering.fitness_percentage:.3f}, "
                f"compression={clustering.compression_ratio:.3f} "
                f"(concurrency-variants/sequence-variants)"
            ),
            "",
        ])

    _with_writer(file_path, _emit)


def write_case_variant_map(
    clustering: ClusteringResult,
    file_path: Union[str, IO],
) -> None:
    """Write a one-row-per-case CSV mapping case ID to variant ID.

    Columns: ``case_id``, ``variant_id`` (``"noise"`` for cases that
    didn't replay). Designed to join cleanly with the user's log on
    ``case_id``."""

    def _emit(writer):
        writer.writerow(["case_id", "variant_id"])
        for v in clustering.variants:
            for cid in v.case_ids:
                writer.writerow([cid, v.variant_id])
        for cid in clustering.noise_case_ids:
            writer.writerow([cid, "noise"])

    _with_writer(file_path, _emit)


# ---------------------------------------------------------------------------
# I/O dispatch
# ---------------------------------------------------------------------------

def _with_writer(file_path: Union[str, IO], fn) -> None:
    if hasattr(file_path, "write"):
        writer = csv.writer(file_path, lineterminator="\n")
        fn(writer)
        return
    with open(file_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        fn(writer)
