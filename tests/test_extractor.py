"""Smoke tests for fortran-acc-audit.

Fixture-based checks that the extractor produces the expected nodes, edges,
and audit verdict for three canonical cases:
  - sample_violation.F  — VL=128 × routine seq  → 1 violation
  - sample_clean.F      — VL=1  × routine seq   → 0 violations
  - sample_host_only.f90— no OpenACC             → 0 violations, 0 acc edges
"""
from __future__ import annotations

from pathlib import Path

from fortran_acc_audit.extractor import (
    discover_sources,
    process_file,
    audit_vl_seq_mismatch,
    FORTRAN_EXTENSIONS,
)

FIX = Path(__file__).parent / "fixtures"


def test_discover_finds_all_three_fixtures():
    files = discover_sources(FIX, recursive=False)
    names = {f.name for f in files}
    assert "sample_violation.F" in names
    assert "sample_clean.F" in names
    assert "sample_host_only.f90" in names
    assert len(files) == 3


def test_extensions_cover_standard_fortran_suffixes():
    # Sanity: the ext list is non-empty and includes all commonly seen suffixes.
    for ext in [".F", ".F90", ".f", ".f90", ".f95", ".f03", ".f08"]:
        assert ext in FORTRAN_EXTENSIONS


def test_violation_fixture_produces_one_finding():
    src = FIX / "sample_violation.F"
    bundle = process_file(src, base_dir=FIX, cpp_on=False)
    assert bundle.get("error") in (None, []), bundle.get("error")
    nodes = bundle["nodes"]
    edges = bundle["edges"]

    # Expected subroutines: driver, column_sub (plus the module)
    names = {n.name for n in nodes}
    assert "driver" in names
    assert "column_sub" in names

    # column_sub must be marked routine seq
    col = next(n for n in nodes if n.name == "column_sub")
    assert col.acc_routine == "seq"

    # There must be at least one acc_kernel_call edge to column_sub
    ack_edges = [e for e in edges if e.kind == "acc_kernel_call"
                 and e.to_name == "column_sub"]
    assert len(ack_edges) >= 1
    # With VL=128
    assert ack_edges[0].caller_context["vector_length"] == 128

    # Audit: exactly one violation
    findings = audit_vl_seq_mismatch(nodes, edges)
    assert len(findings) == 1
    f = findings[0]
    assert f.callee_sub == "column_sub"
    assert f.caller_vector_length == 128
    assert f.severity == "HIGH"


def test_clean_fixture_produces_zero_findings():
    src = FIX / "sample_clean.F"
    bundle = process_file(src, base_dir=FIX, cpp_on=False)
    nodes, edges = bundle["nodes"], bundle["edges"]
    # Audit clean: no violation even though column_sub is routine seq
    assert audit_vl_seq_mismatch(nodes, edges) == []
    # But the acc_kernel_call edge should still exist with vl=1
    ack_edges = [e for e in edges if e.kind == "acc_kernel_call"]
    assert any(e.caller_context["vector_length"] == 1 for e in ack_edges)


def test_host_only_fixture_produces_no_acc_edges():
    src = FIX / "sample_host_only.f90"
    bundle = process_file(src, base_dir=FIX, cpp_on=False)
    nodes, edges = bundle["nodes"], bundle["edges"]
    # No acc_kernel_call edges
    assert all(e.kind == "call" for e in edges), \
        f"expected no acc edges, got {[e for e in edges if e.kind!='call']}"
    # No routine seq nodes
    assert all(n.acc_routine is None for n in nodes)
    # Audit is trivially clean
    assert audit_vl_seq_mismatch(nodes, edges) == []


def test_directory_walk_smoke():
    """End-to-end: scan the fixtures dir as a whole."""
    all_nodes = []
    all_edges = []
    for f in discover_sources(FIX):
        bundle = process_file(f, base_dir=FIX, cpp_on=False)
        all_nodes.extend(bundle["nodes"])
        all_edges.extend(bundle["edges"])
    # Aggregate audit across all 3 fixtures = still 1 finding (only the
    # violation fixture triggers)
    findings = audit_vl_seq_mismatch(all_nodes, all_edges)
    assert len(findings) == 1, f"expected 1, got {len(findings)}: {findings}"
