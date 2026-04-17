"""CLI entry point — exposes `fortran-acc-audit` command after pip install."""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .extractor import (
    discover_sources,
    process_file,
    audit_vl_seq_mismatch,
    FORTRAN_EXTENSIONS,
)
from .schema import (
    nodes_to_dicts,
    edges_to_dicts,
    to_node_link_data,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fortran-acc-audit",
        description=(
            "Static audit for OpenACC directive patterns in Fortran. "
            "Detects idle-lane-tax (routine seq × VL>1) and exposes "
            "subroutine/CALL structure as JSON."),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--source-dir", type=Path, required=True,
                   help="Root directory containing Fortran sources.")
    p.add_argument("--out-dir", type=Path, default=Path("acc_audit_output"),
                   help="Directory to write JSON artifacts.")
    p.add_argument("--recursive", action="store_true",
                   help="Recurse into subdirectories of --source-dir.")
    p.add_argument("--include-backups", action="store_true",
                   help="Include .bak_*, .pre-*, .canonical, .orig snapshots.")
    p.add_argument("--extensions", nargs="+", default=list(FORTRAN_EXTENSIONS),
                   help=f"File extensions to scan (default: {list(FORTRAN_EXTENSIONS)}).")
    p.add_argument("--cpp-define", "-D", action="append", dest="cpp_defines",
                   default=[], metavar="MACRO[=VAL]",
                   help="Preprocessor define (repeatable). Only used on "
                        "uppercase .F/.F90 extensions.")
    p.add_argument("--cpp-include", "-I", action="append", dest="cpp_includes",
                   default=[], metavar="PATH", type=Path,
                   help="Preprocessor include directory (repeatable).")
    p.add_argument("--no-cpp", action="store_true",
                   help="Disable CPP preprocessing even for .F/.F90 (files are "
                        "scanned as-is; CPP is currently only a syntactic sanity "
                        "check, not a transform).")
    p.add_argument("--cpp-exe", default="/lib/cpp",
                   help="Preprocessor executable.")
    p.add_argument("--emit-graphify-layer", type=Path, default=None,
                   metavar="PATH",
                   help="Also write NetworkX node_link_data JSON to PATH "
                        "(opt-in Layer B adapter for merging into graphify/NetworkX).")
    p.add_argument("--fail-on-violation", action="store_true",
                   help="Exit 1 if audit finds any routine-seq × VL>1 violation.")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="Suppress per-file progress.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    files = discover_sources(
        args.source_dir, recursive=args.recursive,
        include_backups=args.include_backups,
        extensions=tuple(args.extensions),
    )
    if not files:
        print(f"no Fortran sources in {args.source_dir}", file=sys.stderr)
        return 2

    t0 = time.time()
    all_nodes = []
    all_edges = []
    all_acc: dict[str, dict] = {}
    file_manifests = []
    errors: list[dict] = []

    for f in files:
        cpp_on = None if not args.no_cpp else False
        bundle = process_file(
            f, base_dir=args.source_dir,
            cpp_defines=args.cpp_defines,
            cpp_includes=args.cpp_includes,
            cpp_on=cpp_on,
            cpp_exe=args.cpp_exe,
        )
        file_manifests.append({
            "file": bundle["file"],
            "elapsed_s": bundle.get("elapsed_s"),
            "node_count": len(bundle.get("nodes", [])),
            "edge_count": len(bundle.get("edges", [])),
            "error": bundle.get("error"),
        })
        if bundle.get("error"):
            # Non-fatal CPP warnings pass through; only skip if no nodes produced
            if not bundle.get("nodes"):
                errors.append({"file": bundle["file"], "error": bundle["error"]})
                if not args.quiet:
                    print(f"  SKIP {bundle['file']}: {bundle['error']}", file=sys.stderr)
                continue
        all_nodes.extend(bundle["nodes"])
        all_edges.extend(bundle["edges"])
        all_acc[bundle["file"]] = asdict(bundle["acc_attrs"])

    elapsed = time.time() - t0

    # Audit
    findings = audit_vl_seq_mismatch(all_nodes, all_edges)

    # Write native outputs (Layer A)
    (args.out_dir / "nodes.json").write_text(
        json.dumps(nodes_to_dicts(all_nodes), indent=2) + "\n")
    (args.out_dir / "edges.json").write_text(
        json.dumps(edges_to_dicts(all_edges), indent=2) + "\n")
    (args.out_dir / "acc_attributes.json").write_text(
        json.dumps(all_acc, indent=2) + "\n")
    (args.out_dir / "audit_vl_seq_mismatch.json").write_text(
        json.dumps([asdict(f) for f in findings], indent=2) + "\n")
    manifest = {
        "tool": "fortran-acc-audit",
        "version": __version__,
        "source_dir": str(args.source_dir),
        "files_scanned": len(files),
        "parse_errors": len(errors),
        "total_nodes": len(all_nodes),
        "total_edges": len(all_edges),
        "audit_findings": len(findings),
        "elapsed_s": round(elapsed, 3),
        "files": file_manifests,
    }
    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    # Write graphify adapter (Layer B, opt-in)
    if args.emit_graphify_layer is not None:
        nld = to_node_link_data(all_nodes, all_edges)
        args.emit_graphify_layer.parent.mkdir(parents=True, exist_ok=True)
        args.emit_graphify_layer.write_text(json.dumps(nld, indent=2) + "\n")

    # Report
    print("== fortran-acc-audit ==")
    print(f"  files scanned  : {len(files)}")
    print(f"  parse errors   : {len(errors)}")
    print(f"  nodes          : {len(all_nodes)}")
    print(f"  edges          : {len(all_edges)}")
    print(f"  audit findings : {len(findings)}")
    print(f"  elapsed        : {elapsed:.2f}s")
    print(f"  output         : {args.out_dir}/")
    if args.emit_graphify_layer:
        print(f"  graphify layer : {args.emit_graphify_layer}")

    if findings:
        print()
        print("  VIOLATIONS (routine seq × VL>1):")
        for i, f in enumerate(findings, 1):
            print(f"  #{i}  {f.caller_file}:{f.caller_acc_pl_line} VL={f.caller_vector_length}")
            print(f"       → CALL {f.callee_sub} at :{f.call_line}")
            print(f"         (callee declared at {f.callee_file}:{f.callee_line_start})")
        if args.fail_on_violation:
            return 1

    if errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
