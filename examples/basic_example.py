"""Hand-built UCM example.

This script constructs a small Use Case Map programmatically, saves it to a
``.jucm`` file, re-imports it, and prints a brief structural summary —
demonstrating that the model survives a full round-trip through the
jUCMNav-compatible XMI exporter and importer.

The map fragment modelled here looks like::

    ● ─► [Login] ─► <X>──[Search]──►(X) ─► [Checkout] ─► |
                     │              │
                     └──[Browse]────┘

i.e. *start → Login → choice (Search / Browse) → Checkout → end*.
"""

from __future__ import annotations

import os
import tempfile

from pm4py_ucm import UCM, read_ucm, write_ucm


def build_demo_ucm() -> UCM:
    ucm = UCM(name="OnlineShop")
    ucm_map = ucm.add_map(name="ShoppingFlow")

    # Responsibilities (re-usable activity definitions).
    r_login = ucm.get_or_add_responsibility("Login")
    r_search = ucm.get_or_add_responsibility("Search")
    r_browse = ucm.get_or_add_responsibility("Browse")
    r_checkout = ucm.get_or_add_responsibility("Checkout")

    # Path nodes.
    start = ucm_map.add_node(UCM.StartPoint(name="start"))
    n_login = ucm_map.add_node(UCM.RespRef(resp_def=r_login))
    fork = ucm_map.add_node(UCM.OrFork(name="choose"))
    n_search = ucm_map.add_node(UCM.RespRef(resp_def=r_search))
    n_browse = ucm_map.add_node(UCM.RespRef(resp_def=r_browse))
    join = ucm_map.add_node(UCM.OrJoin())
    n_checkout = ucm_map.add_node(UCM.RespRef(resp_def=r_checkout))
    end = ucm_map.add_node(UCM.EndPoint(name="end"))

    # Path edges.
    ucm_map.add_connection(start, n_login)
    ucm_map.add_connection(n_login, fork)
    ucm_map.add_connection(fork, n_search, condition="search")
    ucm_map.add_connection(fork, n_browse, condition="browse")
    ucm_map.add_connection(n_search, join)
    ucm_map.add_connection(n_browse, join)
    ucm_map.add_connection(join, n_checkout)
    ucm_map.add_connection(n_checkout, end)
    return ucm


def main() -> None:
    ucm = build_demo_ucm()
    print(ucm)

    out_dir = tempfile.mkdtemp(prefix="pm4py_ucm_example_")
    out_path = os.path.join(out_dir, "online_shop.jucm")
    write_ucm(ucm, out_path)
    print(f"Wrote {out_path}")

    ucm_again = read_ucm(out_path)
    m = ucm_again.maps[0]
    print(f"Re-imported map '{m.name}' with {len(m.nodes)} nodes "
          f"and {len(m.connections)} edges.")
    print("Responsibilities:",
          [r.name for r in ucm_again.responsibilities])


if __name__ == "__main__":
    main()
