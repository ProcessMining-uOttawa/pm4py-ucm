"""Variant discovery and clustering — concurrency-aware trace equivalence.

Submodules
----------

:mod:`choice_signature`
    Replay an event-log trace on a discovered process tree and return a
    canonical *choice signature*: a nested tuple recording which XOR
    branch was taken at every choice point and how many times each loop
    iterated. Two traces that differ only in the interleaving order of
    activities within a parallel block share the same signature, so
    sequence-equivalent variants merge under concurrency-equivalent
    semantics.

:mod:`clustering`
    Cluster a whole event log by choice signature, producing a list of
    named variants (``v1``, ``v2``, …) ordered by frequency, plus a
    fitness percentage (fraction of cases that replayed cleanly).
"""
