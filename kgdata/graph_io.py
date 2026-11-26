"""Load a graph written by scripts/build_graph.py.

    from kgdata.graph_io import load_tables, to_networkx
    nodes, edges = load_tables("graph_NC")          # {type: DataFrame}, {type: DataFrame}
    G = to_networkx(nodes, edges)                   # networkx.MultiDiGraph (needs networkx)
    G = to_networkx(nodes, edges, edge_types=["flows_to"])   # only the river network
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_tables(folder, node_types=None, edge_types=None):
    folder = Path(folder)
    nodes = {f.stem: pd.read_parquet(f) for f in sorted((folder / "nodes").glob("*.parquet"))
             if node_types is None or f.stem in node_types}
    edges = {f.stem: pd.read_parquet(f) for f in sorted((folder / "edges").glob("*.parquet"))
             if edge_types is None or f.stem in edge_types}
    return nodes, edges


def to_networkx(nodes, edges, node_types=None, edge_types=None):
    """Typed MultiDiGraph: node attribute `type`, edge attribute `type`, plus every column."""
    import networkx as nx

    G = nx.MultiDiGraph()
    for t, df in nodes.items():
        if node_types and t not in node_types:
            continue
        attrs = df.drop(columns="id").to_dict("records")
        G.add_nodes_from((i, {"type": t, **a}) for i, a in zip(df["id"], attrs))
    for t, df in edges.items():
        if edge_types and t not in edge_types:
            continue
        cols = [c for c in df.columns if c not in ("src", "dst")]
        attrs = df[cols].to_dict("records") if cols else [{}] * len(df)
        G.add_edges_from((s, d, {"type": t, **a}) for s, d, a in zip(df["src"], df["dst"], attrs))
    return G
