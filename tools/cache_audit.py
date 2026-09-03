"""Audit a Streamlit app for the three ways ``@st.cache_data`` goes wrong.

Run it against the app::

    python tools/cache_audit.py web/streamlit_app_v6.py

Two of these have bitten this project already, and both took a user report to
find, because neither shows up in the test suite:

1. **Excluded-param drift** (silent). A parameter whose name starts with ``_``
   is left OUT of the cache key so Streamlit does not try to hash it. If the
   hashed parameters do not *determine* it, the cache serves a result computed
   for different inputs, with no error. ``_dashboard_html_cached`` keyed on the
   log's file hash while its fact table came from the *filtered* log.

2. **The copy boundary** (silent). ``cache_data`` pickles values in and out, so
   a cached value and a live one are separate copies. Anything keyed on object
   identity, or on state allocated lazily on first access, disagrees across
   that boundary — which is how scenario coverage came to report elements "not
   in this model" (issue #115).

3. **Element replay** (loud, but only on the *second* call). Streamlit records
   the elements a cached function draws so it can replay them on a hit. An
   element created on a block that was made OUTSIDE the function replays into
   a block that no longer exists: ``CacheReplayClosureError`` (issue #120).

Classes 1 and 3 are checked here. Class 2 cannot be found statically — it needs
the reviewer to ask, of any value crossing the cache, whether it means the same
thing in a copy as it did in the original.

Exits non-zero if it finds a class-3 violation or a call site relying on a
default for a cache-key parameter.
"""

import ast, pathlib, sys

APP = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                   else "web/streamlit_app_v6.py")
tree = ast.parse(APP.read_text(encoding="utf-8"))
UI = {"_status", "_progress"}
ELEMENTS = {"write","markdown","dataframe","metric","caption","info","warning",
            "error","success","progress","image","table","json","code","text",
            "subheader","header","title","container","expander","columns",
            "tabs","empty","status","spinner","button","selectbox","radio",
            "multiselect","checkbox","slider","text_input","number_input",
            "download_button","toggle","form","plotly_chart","altair_chart"}

funcs = {}
for n in ast.walk(tree):
    if isinstance(n, ast.FunctionDef) and any(
            "cache_data" in ast.unparse(d) or "cache_resource" in ast.unparse(d)
            for d in n.decorator_list):
        funcs[n.name] = n

print(f"{len(funcs)} cached functions\n")

# --- 3. element replay -------------------------------------------------
loud = []
for name, n in funcs.items():
    hits = sorted({c.func.attr for c in ast.walk(n)
                   if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                   and c.func.attr in ELEMENTS})
    if hits:
        loud.append((name, n.lineno, hits))
print("[3] LOUD -- cached functions touching Streamlit elements:",
      len(loud) or "none")
for x in loud:
    print("   ", x)

# --- 1. excluded data params -------------------------------------------
print("\n[1] SILENT -- excluded params carrying data, and the key each call supplies:")
unresolved = []
for name, n in sorted(funcs.items()):
    pos = [a.arg for a in n.args.args]
    kwonly = [a.arg for a in n.args.kwonlyargs]
    allp = pos + kwonly
    data_excl = [p for p in allp if p.startswith("_") and p not in UI
                 and any(isinstance(x, ast.Name) and x.id == p for x in ast.walk(n))]
    if not data_excl:
        continue
    hashed = [p for p in allp if not p.startswith("_")]
    sites = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
             and isinstance(c.func, ast.Name) and c.func.id == name]
    lines = []
    for c in sites:
        bound = set()
        for i, _a in enumerate(c.args):
            if i < len(pos):
                bound.add(pos[i])
        star = any(kw.arg is None for kw in c.keywords)   # **kwargs unpacking
        for kw in c.keywords:
            if kw.arg:
                bound.add(kw.arg)
        missing = [h for h in hashed if h not in bound]
        if missing and not star:
            lines.append(f"      L{c.lineno}: key params not supplied -> {missing}")
            unresolved.append((name, c.lineno, missing))
        elif missing and star:
            lines.append(f"      L{c.lineno}: {missing} supplied via ** unpacking")
    print(f"   {name:32s} key={len(hashed)} excluded={data_excl}")
    for l in lines:
        print(l)

print("\n" + "=" * 72)
print("call sites silently relying on a DEFAULT for a cache-key param:",
      len(unresolved) or "none")
for u in unresolved:
    print("   ", u)

sys.exit(1 if (loud or unresolved) else 0)
