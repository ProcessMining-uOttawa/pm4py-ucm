# pm4py-ucm web front-end

A thin Streamlit app over the `pm4py-ucm` package. v1 does just one thing:
upload an XES log, mine a UCM with the inductive miner, render it, and offer
the `.jucm` for download. Notation toggle, decomposition, performer config,
and CSV import are planned follow-ups.

## Run locally

Requires Python 3.9+ and the [Graphviz](https://graphviz.org/download/) `dot`
binary on `PATH` (the layouter shells out to it).

From the repo root:

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS/Linux

pip install -r web/requirements.txt
streamlit run web/streamlit_app.py
```

Streamlit opens `http://localhost:8501`.

The web-specific deps (`streamlit`, `pm4py`) live in `web/requirements.txt`
so they stay out of the package's own dev workflow (`pip install -e ".[dev]"`).

## Deploy to Streamlit Community Cloud

1. Push this repo to GitHub.
2. At <https://share.streamlit.io>, "New app" -> pick the repo and branch.
3. Set **Main file path** to `web/streamlit_app.py`. Streamlit Cloud picks up
   `web/requirements.txt` and `web/packages.txt` automatically because they
   sit next to the main file. If a build ever fails to find them, point at
   them explicitly under Advanced settings.
4. Deploy. `packages.txt` installs the `graphviz` apt package, which provides
   the `dot` binary the layouter shells out to.

Updates are a `git push` to the tracked branch.

## Layout

```
web/
  streamlit_app.py    # entry point
  requirements.txt    # streamlit + pm4py + `-e .` (the package itself)
  packages.txt        # apt: graphviz
```

`-e .` is a relative path resolved against pip's working directory, which is
the repo root both locally (`pip install -r web/requirements.txt` run from
the repo root) and on Streamlit Cloud. Either way it points at the
`pm4py-ucm` project, so any change to the library is picked up on the next
Streamlit rerun (locally) or next deploy (on the Cloud).
