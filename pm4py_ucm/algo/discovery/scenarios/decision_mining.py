"""Data-driven OR-fork condition discovery via decision-tree mining.

Given an event log and per-case branch labels at each XOR/OR fork,
this module:

1. Extracts every case-level attribute the log carries — including
   event-level columns that happen to be **constant per case**
   (typical in real-world XES exports, where business properties
   like ``Channel`` or ``Claim_Value`` are repeated on every event
   instead of being lifted into the ``case:`` prefix).
2. Classifies each attribute into a jUCMNav-compatible type:

   * ``boolean`` — for ``{True, False}`` attributes (native bool,
     ``0``/``1``, or ``"true"``/``"false"`` strings in any case);
   * ``integer`` — for integer-valued numerics;
   * ``integer`` with a scale factor — for real numbers, multiplied
     by the smallest power of 10 that makes every observed value
     integral (the factor is recorded so per-scenario
     initialisations can apply it consistently);
   * ``enumeration`` — for low-cardinality strings (≤
     ``max_enum_cardinality``).

   High-cardinality strings, timestamps, dates and constants are
   dropped.
3. Trains a small ``sklearn.DecisionTreeClassifier`` per outside-
   loop XOR predicting branch index from the case-level features.
4. Walks the resulting tree to emit a jUCMNav-compatible condition
   expression per branch, using the existing syntax conventions
   (bare enum identifiers, ``&&``, ``||``, ``==``, ``!=``, no
   string quoting).

When the log carries no usable case-constant attributes, the
module returns a :data:`NO_USEFUL_ATTRIBUTES` sentinel so the
synthesizer can abandon data-driven mining for this run and emit
a warning — there is no fallback to the variant-driven encoding
in this mode.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
import math
import re
import warnings


#: Sentinel returned by :func:`extract_case_features` when the log
#: carries no attributes worth mining (only IDs, activities,
#: timestamps, and high-cardinality strings). Synthesizer treats it
#: as "abandon data-driven mining with a warning".
NO_USEFUL_ATTRIBUTES = "NO_USEFUL_ATTRIBUTES"


def _boolean_value(v):
    """Interpret ``v`` as a boolean literal, **case-insensitively** for
    strings.

    Recognises native ``bool``, the numbers ``0`` / ``1``, and the
    strings ``"true"`` / ``"false"`` / ``"0"`` / ``"1"`` in any case — so
    ``"True"``, ``"FALSE"`` and ``"TRUE"`` all classify. Returns ``None``
    when ``v`` is not boolean-like (used both to classify a column as
    boolean and to encode its values for sklearn)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().casefold()
        if s in ("true", "1"):
            return True
        if s in ("false", "0"):
            return False
        return None
    try:
        if v == 1:
            return True
        if v == 0:
            return False
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Attribute specs
# ---------------------------------------------------------------------------

@dataclass
class AttributeSpec:
    """How a single case-level attribute is encoded for sklearn and
    for the jUCMNav expression / variable layer.

    Attributes
    ----------
    source_name
        Original column name in the event log (e.g.
        ``"Claim_Value"`` or ``"case:amount"``).
    jucmnav_name
        Sanitised variable name suitable for the jUCMNav
        expression grammar (alphanumeric + underscore, no
        ``case:`` prefix).
    type
        One of ``"boolean"``, ``"integer"``, ``"enumeration"`` —
        the jUCMNav type discriminator the synthesizer will emit
        on the :class:`UCM.Variable`.
    enum_values
        For ``"enumeration"`` only: the ordered list of values
        seen across cases, **already sanitised** into valid
        jUCMNav identifiers (no spaces, dots, or other special
        characters). Used both to populate
        :class:`UCM.EnumerationType.values` and to emit
        ``attr == VALUE`` literals in conditions.
    enum_value_mapping
        For ``"enumeration"`` only: maps each sanitised value
        back to the original raw value seen in the log. Lets the
        per-OR-fork report explain what each enum literal stands
        for, and lets scenario initialisations turn a case's raw
        attribute value into its sanitised counterpart.
    scale_factor
        For ``"integer"`` derived from a real-number column: the
        power of ten by which raw values are multiplied to obtain
        the integer feature. ``1`` for naturally-integer columns,
        ``10`` for one-decimal floats, ``100`` for two-decimal,
        etc. Documented in the per-OR-fork mining report so users
        understand what scenario initialisations encode.
    """

    source_name: str
    jucmnav_name: str
    type: str
    enum_values: List[str] = field(default_factory=list)
    enum_value_mapping: Dict[str, str] = field(default_factory=dict)
    scale_factor: int = 1

    def sanitise_value(self, raw) -> str:
        """Map a raw enum value back to its sanitised jUCMNav literal.

        Falls back to a fresh sanitisation when the value wasn't
        observed at extraction time (e.g. a new value appears in
        a downstream scenario init that wasn't in the training
        data). Returns the raw value unchanged for non-enum
        attributes."""
        if self.type != "enumeration":
            return str(raw)
        for sanitised, original in self.enum_value_mapping.items():
            if original == str(raw):
                return sanitised
        return _sanitise_jucmnav_name(str(raw))


@dataclass
class OrForkMiningResult:
    """Mined condition for one OR-fork (or a "skipped" marker).

    Attributes
    ----------
    tree_xor_id
        Stable signature ID of the XOR tree node this corresponds
        to (matches the IDs used by clustering / synthesis).
    n_branches
        Number of outgoing branches of the OR-fork in the UCM.
    accuracy
        Training accuracy of the per-fork decision tree on the
        case→branch labels. Self-explanatory metric: how often
        the tree predicts the branch the case actually took. No
        accuracy threshold is applied — the user sees the number
        and decides whether to trust the model.
    n_samples
        Number of cases that contributed a branch label at this
        fork (cases that didn't reach the fork are excluded).
    features_used
        ``jucmnav_name`` of every attribute the trained tree
        actually split on. Empty when the tree is a single leaf
        (one branch dominates with no useful split).
    per_branch_expression
        ``{branch_index: jucmnav_expression}``. Branches the tree
        never predicted get ``"false"``; branches the tree always
        predicts get ``"true"``. Combined across branches the
        expressions are mutually exclusive and jointly exhaustive
        as long as the tree itself partitions feature space.
    skipped_reason
        Set when this fork was excluded from data-driven mining
        (e.g. ``"inside_loop"``); ``None`` otherwise.
    """

    tree_xor_id: int
    n_branches: int
    accuracy: float = 0.0
    n_samples: int = 0
    features_used: List[str] = field(default_factory=list)
    per_branch_expression: Dict[int, str] = field(default_factory=dict)
    skipped_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Case-feature extraction
# ---------------------------------------------------------------------------

#: Columns we always skip regardless of constancy: activity, case-id,
#: standard timestamp variants. They never help the decision tree.
_ALWAYS_SKIP = {
    "concept:name", "case:concept:name",
    "time:timestamp", "timestamp", "start_timestamp", "AssignedTime",
}


def _sanitise_jucmnav_name(raw: str) -> str:
    """Convert a raw column name or enumeration value into a valid
    jUCMNav identifier.

    jUCMNav's grammar requires identifiers (variable names and
    enumeration values alike) to start with a letter or underscore;
    a leading digit makes the scenario tool reject the model.
    Examples that hit this: numeric country codes like ``"3"``, or
    bucketed labels like ``"3_5"``.

    Steps:

    1. Strip a leading ``case:`` prefix.
    2. Replace non-word characters with underscores.
    3. Collapse runs of underscores and trim trailing underscores
       (leading ones are preserved -- they're legal *and* might be
       added back in step 4).
    4. If the first character is a digit, prepend a single
       underscore so the result satisfies the grammar.
    5. Fall back to ``"attr"`` if everything got stripped away.
    """
    name = raw[len("case:"):] if raw.startswith("case:") else raw
    name = re.sub(r"\W+", "_", name)
    name = re.sub(r"_+", "_", name).rstrip("_")
    # ``lstrip("_")`` would drop the safety underscore we may add
    # below, so trim only the leading underscores that *aren't*
    # followed by a digit -- if the result still starts ``_<digit>``
    # after trimming, that's fine, the leading underscore is what
    # makes the identifier legal.
    while name.startswith("_") and len(name) > 1 and not name[1].isdigit():
        name = name[1:]
    if not name:
        return "attr"
    if name[0].isdigit():
        name = "_" + name
    return name


def _infer_scale_factor(values: Sequence[float], max_power: int = 6) -> int:
    """Return the smallest power of 10 that makes every value in
    ``values`` integer-valued (within float tolerance).

    Capped at ``10^max_power`` so a single rogue value can't blow
    out the factor — better to truncate one weird sample than emit
    integer thresholds in the millions for the common case."""
    max_digits = 0
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(f) or math.isinf(f):
            continue
        if f == int(f):
            continue
        # Format with enough precision, strip trailing zeros.
        s = f"{f:.10f}".rstrip("0")
        if "." in s:
            decimals = len(s.rsplit(".", 1)[1])
            max_digits = max(max_digits, decimals)
    return 10 ** min(max_digits, max_power)


def extract_case_features(
    log_df,
    case_id_col: str = "case:concept:name",
    constancy_threshold: float = 0.99,
    max_enum_cardinality: int = 20,
) -> Tuple[Any, Dict[str, AttributeSpec], List[str], Any]:
    """Pull every case-constant attribute out of ``log_df``,
    classify each into a jUCMNav-compatible type, and return a
    per-case feature DataFrame ready for sklearn.

    Returns
    -------
    (features_df, attribute_specs, sanitised_names)
        ``features_df`` is indexed by case ID and has one numeric
        column per encoded feature. Enumerations are one-hot
        expanded so the tree can split on any single value; real
        numbers are scaled to integers. ``attribute_specs`` maps
        each *sanitised* jUCMNav variable name to its
        :class:`AttributeSpec`. ``sanitised_names`` is the list
        of feature column names in stable order.

        Returns ``(None, {}, [])`` and emits a warning when no
        usable attribute survives the type / cardinality filters
        — the synthesizer treats this as
        :data:`NO_USEFUL_ATTRIBUTES`.
    """
    import pandas as pd  # local import keeps the dependency soft

    if case_id_col not in log_df.columns:
        warnings.warn(
            f"case id column {case_id_col!r} not in log; "
            f"decision mining abandoned."
        )
        return None, {}, [], None

    # Identify case-constant columns: ones that vary on at most
    # 1 - constancy_threshold of cases. Real-world XES exports often
    # put case-level fields at event level (constant across the
    # case's events), so we lift those into the feature set.
    n_cases = log_df[case_id_col].nunique()
    candidate_cols: List[str] = []
    for col in log_df.columns:
        if col == case_id_col or col in _ALWAYS_SKIP:
            continue
        series = log_df[col]
        if str(series.dtype).startswith("datetime"):
            continue
        distinct_per_case = series.groupby(log_df[case_id_col]).nunique(
            dropna=True,
        )
        constant_share = (distinct_per_case <= 1).sum() / n_cases
        if constant_share >= constancy_threshold:
            candidate_cols.append(col)

    if not candidate_cols:
        warnings.warn(
            "No case-constant attributes survive the constancy "
            "filter; data-driven OR-fork mining abandoned."
        )
        return None, {}, [], None

    # By name, not by the frame's column order. ``pm4py.read_xes`` does
    # not fix that order between runs, and everything downstream
    # inherits it: the URN variables are created in it, and — the part
    # that changes the model — so are the one-hot feature columns the
    # decision tree sees, which is what settles a tie between two
    # equally-informative splits. Mining the same log twice could
    # therefore mine two different conditions (on ClaimsPaymentLog, a
    # branch guarded by one broker in one run and by another in the
    # next, at identical accuracy). Sorting makes the mined model a
    # function of the log's content and nothing else.
    candidate_cols.sort()

    # Take the first row per case as the canonical value snapshot.
    per_case = log_df.drop_duplicates(
        subset=case_id_col, keep="first",
    ).set_index(case_id_col)[candidate_cols]

    # Classify each column.
    attribute_specs: Dict[str, AttributeSpec] = {}
    columns_to_drop: List[str] = []
    for col in candidate_cols:
        series = per_case[col]
        nunique = series.nunique(dropna=True)
        if nunique <= 1:
            columns_to_drop.append(col)
            continue  # constants carry no information
        # Booleans: every value is a boolean literal — native bool,
        # 0/1, or "true"/"false" in ANY case ("True", "FALSE", "TRUE"),
        # with both truth values present. Case-insensitive so mixed
        # spellings still classify as a boolean variable (issue #6)
        # rather than a two-value enumeration.
        non_null = series.dropna()
        bool_values = {_boolean_value(v) for v in non_null.unique()}
        if bool_values == {True, False}:
            spec = AttributeSpec(
                source_name=col,
                jucmnav_name=_sanitise_jucmnav_name(col),
                type="boolean",
            )
            attribute_specs[spec.jucmnav_name] = spec
            continue
        # Numerics: integer-valued or floats.
        if str(series.dtype).startswith(("int", "float")):
            scale = _infer_scale_factor(non_null.tolist())
            spec = AttributeSpec(
                source_name=col,
                jucmnav_name=_sanitise_jucmnav_name(col),
                type="integer",
                scale_factor=scale,
            )
            attribute_specs[spec.jucmnav_name] = spec
            continue
        # Strings: enumeration if cardinality is low enough.
        if nunique <= max_enum_cardinality:
            raw_values = sorted(str(v) for v in non_null.unique())
            # Sanitise each enum value into a jUCMNav identifier
            # (no spaces, dots, etc.). De-duplicate via a counter
            # suffix if two raw values sanitise to the same thing.
            sanitised: List[str] = []
            mapping: Dict[str, str] = {}
            seen: set = set()
            for v in raw_values:
                base = _sanitise_jucmnav_name(v) or "VAL"
                cand = base
                i = 2
                while cand in seen:
                    cand = f"{base}_{i}"
                    i += 1
                seen.add(cand)
                sanitised.append(cand)
                mapping[cand] = v
            spec = AttributeSpec(
                source_name=col,
                jucmnav_name=_sanitise_jucmnav_name(col),
                type="enumeration",
                enum_values=sanitised,
                enum_value_mapping=mapping,
            )
            attribute_specs[spec.jucmnav_name] = spec
            continue
        # Fall through: high-cardinality string, drop.
        columns_to_drop.append(col)

    if not attribute_specs:
        warnings.warn(
            "No case-constant attributes met the type / cardinality "
            "filters; data-driven OR-fork mining abandoned."
        )
        return None, {}, [], None

    # Build a feature DataFrame, encoded for sklearn.
    feature_columns: List[Tuple[str, str]] = []  # (jucmnav_name, encoded_col_name)
    encoded = {}
    for jname, spec in attribute_specs.items():
        col = spec.source_name
        series = per_case[col]
        if spec.type == "boolean":
            encoded[jname] = series.map(_boolean_value).map(
                lambda b: None if b is None else (1 if b else 0)
            ).astype("float")
            feature_columns.append((jname, jname))
        elif spec.type == "integer":
            encoded[jname] = (series.astype("float") * spec.scale_factor).round()
            feature_columns.append((jname, jname))
        elif spec.type == "enumeration":
            # spec.enum_values is sanitised; compare against the
            # original raw value via the mapping. One-hot column
            # name uses the sanitised form so downstream
            # tree-to-expression conversion can split it apart
            # via the ``__`` delimiter.
            for sanitised in spec.enum_values:
                raw_val = spec.enum_value_mapping.get(sanitised, sanitised)
                col_name = f"{jname}__{sanitised}"
                encoded[col_name] = (series == raw_val).astype("float")
                feature_columns.append((jname, col_name))

    features_df = pd.DataFrame(encoded, index=per_case.index)
    # Keep just the columns we actually kept as attributes — drops
    # the constants / high-cardinality ones the caller doesn't need.
    kept_source_cols = [
        spec.source_name for spec in attribute_specs.values()
    ]
    per_case_raw = per_case[kept_source_cols]
    return (
        features_df,
        attribute_specs,
        [c for _, c in feature_columns],
        per_case_raw,
    )


# ---------------------------------------------------------------------------
# Decision-tree mining and expression conversion
# ---------------------------------------------------------------------------

def mine_orfork_condition(
    features_df,
    attribute_specs: Dict[str, AttributeSpec],
    feature_columns: List[str],
    case_to_branch: Dict[str, int],
    tree_xor_id: int,
    n_branches: int,
    max_depth: int = 3,
) -> OrForkMiningResult:
    """Train a small decision tree on the case-level features
    predicting branch index, then translate it into per-branch
    jUCMNav expressions.

    ``case_to_branch`` maps case-id strings to the branch index
    each case took at this OR-fork. Cases not in the dict are
    excluded from training (they didn't reach this fork — likely
    because their variant skipped it via an upstream XOR)."""
    try:
        from sklearn.tree import DecisionTreeClassifier
    except ImportError as exc:  # pragma: no cover - depends on env
        raise ImportError(
            "Data-driven OR-fork mining requires scikit-learn; "
            "install it with `pip install scikit-learn`."
        ) from exc

    # Align features with the labelled cases. Sorted, not in
    # ``case_to_branch``'s insertion order: that order is whatever the
    # caller happened to walk the log in, and it would otherwise decide
    # the order of the training rows — and with them anything the
    # classifier settles by scan order. The trained tree should depend
    # on which cases took which branch, not on how they were collected.
    labelled_ids = sorted(
        cid for cid in case_to_branch if cid in features_df.index
    )
    if not labelled_ids:
        return OrForkMiningResult(
            tree_xor_id=tree_xor_id,
            n_branches=n_branches,
            skipped_reason="no_labelled_cases",
            per_branch_expression={k: "false" for k in range(n_branches)},
        )
    X = features_df.loc[labelled_ids, feature_columns].fillna(0).values
    y = [case_to_branch[cid] for cid in labelled_ids]

    clf = DecisionTreeClassifier(max_depth=max_depth, random_state=0)
    clf.fit(X, y)
    accuracy = float(clf.score(X, y))

    per_branch = tree_to_jucmnav_expressions(
        clf.tree_, feature_columns, attribute_specs, n_branches,
        clf.classes_,
    )

    used_features = _features_used_in_tree(clf.tree_)
    used_jucmnav_names: List[str] = []
    for fi in used_features:
        col = feature_columns[fi]
        # Map back to the attribute spec via the column-naming convention.
        for jname in attribute_specs:
            if col == jname or col.startswith(jname + "__"):
                if jname not in used_jucmnav_names:
                    used_jucmnav_names.append(jname)
                break

    return OrForkMiningResult(
        tree_xor_id=tree_xor_id,
        n_branches=n_branches,
        accuracy=accuracy,
        n_samples=len(labelled_ids),
        features_used=used_jucmnav_names,
        per_branch_expression=per_branch,
    )


def _features_used_in_tree(tree) -> List[int]:
    """Return the feature indices the tree actually splits on (de-duped)."""
    seen: List[int] = []
    for i in range(tree.node_count):
        if tree.children_left[i] == tree.children_right[i]:
            continue  # leaf
        fi = int(tree.feature[i])
        if fi not in seen:
            seen.append(fi)
    return seen


def tree_to_jucmnav_expressions(
    sklearn_tree,
    feature_columns: List[str],
    attribute_specs: Dict[str, AttributeSpec],
    n_branches: int,
    classes: Sequence[int],
) -> Dict[int, str]:
    """Walk an sklearn DecisionTree and emit per-class jUCMNav
    expressions.

    For each leaf, build the path condition by AND-ing the splits
    that led to it. Then, for each predicted class (branch index),
    OR the leaf conditions that predict that class. Branches the
    tree never predicts get ``"false"``; branches the tree always
    predicts (and the tree is a single leaf) get ``"true"``.
    """
    # Pre-walk the tree to collect (leaf_path_conditions, predicted_class).
    paths: List[Tuple[List[str], int]] = []

    def _walk(node: int, conditions: List[str]) -> None:
        if sklearn_tree.children_left[node] == sklearn_tree.children_right[node]:
            value = sklearn_tree.value[node][0]
            predicted_class = int(classes[int(value.argmax())])
            paths.append((list(conditions), predicted_class))
            return
        fi = int(sklearn_tree.feature[node])
        threshold = float(sklearn_tree.threshold[node])
        column = feature_columns[fi]
        left_expr, right_expr = _split_to_expressions(
            column, threshold, attribute_specs,
        )
        conditions.append(left_expr)
        _walk(int(sklearn_tree.children_left[node]), conditions)
        conditions.pop()
        conditions.append(right_expr)
        _walk(int(sklearn_tree.children_right[node]), conditions)
        conditions.pop()

    _walk(0, [])

    per_branch: Dict[int, str] = {k: "false" for k in range(n_branches)}
    for k in range(n_branches):
        clauses: List[str] = []
        for conditions, predicted in paths:
            if predicted != k:
                continue
            if not conditions:
                clauses.append("true")
            elif len(conditions) == 1:
                clauses.append(conditions[0])
            else:
                clauses.append("(" + " && ".join(conditions) + ")")
        if not clauses:
            per_branch[k] = "false"
        elif len(clauses) == 1:
            per_branch[k] = clauses[0]
        else:
            per_branch[k] = " || ".join(clauses)
    return per_branch


def _split_to_expressions(
    column: str,
    threshold: float,
    attribute_specs: Dict[str, AttributeSpec],
) -> Tuple[str, str]:
    """Translate one sklearn split into a ``(left_expr, right_expr)``
    pair of jUCMNav-compatible boolean expressions. ``left_expr``
    holds when sklearn's ``feature <= threshold`` rule fires;
    ``right_expr`` is its negation."""
    # Enumeration one-hot column? Names look like ``attr__VALUE``
    # where VALUE is already the sanitised jUCMNav literal.
    if "__" in column:
        jname, _, val_token = column.partition("__")
        if jname in attribute_specs and attribute_specs[jname].type == "enumeration":
            # left: feature == 0 (i.e. attr != VALUE); right: attr == VALUE.
            return (
                f"{jname} != {val_token}",
                f"{jname} == {val_token}",
            )
    # Boolean
    if column in attribute_specs and attribute_specs[column].type == "boolean":
        return (f"{column} == false", f"{column} == true")
    # Integer (possibly scaled)
    if column in attribute_specs and attribute_specs[column].type == "integer":
        # sklearn's "<= threshold" splits on a real number that's the
        # midpoint between two observed feature values. We round so
        # the literal is a plain integer in jUCMNav.
        t = int(math.floor(threshold))
        return (
            f"{column} <= {t}",
            f"{column} > {t}",
        )
    # Fallback — emit literal threshold as-is.
    return (f"{column} <= {threshold:.4f}", f"{column} > {threshold:.4f}")
