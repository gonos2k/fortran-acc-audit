"""Node / edge schema + JSON serialization + NetworkX node_link_data adapter.

Layer A: native schema used by the audit tool. Stable, versioned.
Layer B (opt-in): NetworkX `node_link_data` adapter for users who want to
merge audit output into a larger graph (e.g. graphify-out/graph.json).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "0.1.0"


@dataclass
class Node:
    """Subroutine, function, or module."""
    id: str                          # canonical ID: "relpath::name"
    kind: str                        # "subroutine" | "function" | "module"
    name: str
    file: str                        # relative path (repo-relative or as given)
    line_start: int
    line_end: int
    module: str | None = None        # containing module name (None for top-level)
    acc_routine: str | None = None   # "seq" | "worker" | "vector" | None


@dataclass
class CallerContext:
    """OpenACC context for a CALL statement inside a parallel region."""
    acc_parallel_loop_line: int
    vector_length: int | None        # None = directive present but VL unspecified
    has_vl_directive: bool
    region_end_line: int


@dataclass
class Edge:
    """CALL caller -> callee. Kind = 'call' (host) or 'acc_kernel_call' (in
    parallel region)."""
    from_: str                       # caller node id (field renamed to avoid py keyword)
    from_sub: str
    to_name: str                     # callee name (lowercased; cross-file resolution by name match)
    call_line: int
    kind: str                        # "call" | "acc_kernel_call"
    caller_context: dict | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["from"] = d.pop("from_")
        return d


@dataclass
class AccAttributes:
    """Per-file overlay of OpenACC directives (raw-text scan)."""
    routine_directives: list[dict] = field(default_factory=list)
    parallel_loops: list[dict] = field(default_factory=list)


@dataclass
class AuditFinding:
    """A single routine-seq × VL>1 caller violation."""
    caller_file: str
    caller_sub: str
    caller_acc_pl_line: int
    caller_vector_length: int
    call_line: int
    callee_sub: str
    callee_file: str
    callee_line_start: int
    severity: str                    # "HIGH" | "MEDIUM" | "LOW"
    rationale: str


def nodes_to_dicts(nodes: list[Node]) -> list[dict]:
    return [asdict(n) for n in nodes]


def edges_to_dicts(edges: list[Edge]) -> list[dict]:
    return [e.to_dict() for e in edges]


def to_node_link_data(nodes: list[Node], edges: list[Edge]) -> dict:
    """NetworkX node_link_data format (Layer B adapter).

    Compatible with networkx.readwrite.json_graph.node_link_graph(data,
    edges='links') and thus mergeable with graphify-out/graph.json.

    Preserves fortran_acc_audit-specific fields as extra keys; consumers that
    don't recognize them can ignore them. Node `id` stays as canonical
    'relpath::name' to avoid collisions when merged with a multi-language
    graph.
    """
    node_list = []
    for n in nodes:
        d = asdict(n)
        # NetworkX requires 'id' — already present
        node_list.append(d)
    link_list = []
    for e in edges:
        d = e.to_dict()
        # NetworkX expects 'source' and 'target' by default in node_link_data;
        # we supply both the canonical graphify convention (`source`, `target`)
        # and preserve the native `from`/`to_name` fields.
        d_nld = {
            "source": d["from"],
            "target": f"name::{d['to_name']}",  # name-only ref; merge consumer resolves
            "call_line": d["call_line"],
            "kind": d["kind"],
            "caller_context": d["caller_context"],
            "from_sub": d["from_sub"],
            "to_name": d["to_name"],
        }
        link_list.append(d_nld)
    return {
        "directed": True,
        "multigraph": True,
        "graph": {
            "produced_by": "fortran-acc-audit",
            "schema_version": SCHEMA_VERSION,
            "layer": "fortran_openacc",
        },
        "nodes": node_list,
        "links": link_list,
    }
