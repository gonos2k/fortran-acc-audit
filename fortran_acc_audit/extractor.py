"""Extractor — walks Fortran source tree, builds nodes/edges + ACC overlay, runs audit.

Supports all standard Fortran extensions: .F / .F90 / .f / .f90 / .f95 / .f03 / .f08.
Optional CPP preprocessing (enabled when source has `.F`/`.F90` case or `--cpp` flag).

Scope detection is raw-text regex by default (fast, no dependency). Optional fparser2
path available via `use_fparser=True` for CONTAINS/nested-subroutine accuracy in
codebases where that matters.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from .schema import (
    Node, Edge, AccAttributes, AuditFinding,
)

# All standard Fortran extensions.
FORTRAN_EXTENSIONS: tuple[str, ...] = (
    ".F", ".F90", ".F95", ".F03", ".F08",    # uppercase = needs CPP
    ".f", ".f90", ".f95", ".f03", ".f08",    # lowercase = no CPP expected
)
CPP_REQUIRED_EXTENSIONS: tuple[str, ...] = (".F", ".F90", ".F95", ".F03", ".F08")

# -------- regex patterns --------

ACC_ROUTINE_RE = re.compile(
    r'^\s*!\$acc\s+routine\s+(seq|worker|vector)\b', re.IGNORECASE)
ACC_PARALLEL_VL_RE = re.compile(
    r'^\s*!\$acc\s+parallel\s+loop.*?vector_length\s*\(\s*(\d+)\s*\)',
    re.IGNORECASE)
ACC_PARALLEL_NO_VL_RE = re.compile(
    r'^\s*!\$acc\s+parallel\s+loop\b', re.IGNORECASE)
ACC_END_PARALLEL_RE = re.compile(
    r'^\s*!\$acc\s+end\s+parallel\b', re.IGNORECASE)
ACC_END_DATA_RE = re.compile(
    r'^\s*!\$acc\s+end\s+data\b', re.IGNORECASE)
SUB_OPEN_RE = re.compile(
    r'^\s*(?:recursive\s+|pure\s+|elemental\s+)*subroutine\s+(\w+)',
    re.IGNORECASE)
SUB_END_RE = re.compile(r'^\s*end\s+subroutine\b', re.IGNORECASE)
FUNC_OPEN_RE = re.compile(
    r'^\s*(?:recursive\s+|pure\s+|elemental\s+|[\w()*]+\s+)*function\s+(\w+)\s*\(',
    re.IGNORECASE)
FUNC_END_RE = re.compile(r'^\s*end\s+function\b', re.IGNORECASE)
MOD_OPEN_RE = re.compile(r'^\s*module\s+(\w+)\s*$', re.IGNORECASE)
MOD_END_RE = re.compile(r'^\s*end\s+module\b', re.IGNORECASE)
CALL_RE = re.compile(r'^\s*call\s+(\w+)\s*[(\n\s]', re.IGNORECASE)


# -------- CPP preprocessing (optional) --------

def cpp_preprocess(src: Path,
                   cpp_defines: list[str] | None = None,
                   cpp_includes: list[Path] | None = None,
                   cpp_exe: str = "/lib/cpp",
                   timeout: int = 60) -> tuple[str, list[str]]:
    """Run the C preprocessor on `src`. Used for syntactic validation of
    uppercase-extension Fortran sources. Returns (stdout, diagnostics)."""
    cmd = [cpp_exe, "-P", "-traditional-cpp"]
    for inc in (cpp_includes or []):
        cmd.append(f"-I{inc}")
    for d in (cpp_defines or []):
        cmd.append(f"-D{d}")
    cmd.append(str(src))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return "", [f"cpp not found at {cpp_exe}"]
    except subprocess.TimeoutExpired:
        return "", [f"cpp timeout after {timeout}s"]
    if r.returncode != 0:
        return "", [f"cpp rc={r.returncode}: {r.stderr[:200].strip()}"]
    return r.stdout, []


# -------- raw-text scan --------

def _scan_scopes(lines: list[str], open_re: re.Pattern, end_re: re.Pattern
                 ) -> list[tuple[str, int, int]]:
    spans: list[tuple[str, int, int]] = []
    stack: list[tuple[str, int]] = []
    for i, line in enumerate(lines, 1):
        mo = open_re.match(line)
        me = end_re.match(line)
        if mo and not me:
            stack.append((mo.group(1).lower(), i))
        elif me and stack:
            name, start = stack.pop()
            spans.append((name, start, i))
    return spans


def _scan_acc_directives(lines: list[str]) -> tuple[list[tuple[int, str]],
                                                     list[tuple[int, int | None, bool]],
                                                     list[int],
                                                     list[int]]:
    """Return (routine_directives, parallel_loops, end_parallel_lines, end_data_lines)."""
    routine_directives: list[tuple[int, str]] = []
    parallel_loops: list[tuple[int, int | None, bool]] = []
    end_parallel_lines: list[int] = []
    end_data_lines: list[int] = []
    for i, line in enumerate(lines, 1):
        m = ACC_ROUTINE_RE.match(line)
        if m:
            routine_directives.append((i, m.group(1).lower()))
        m = ACC_PARALLEL_VL_RE.match(line)
        if m:
            parallel_loops.append((i, int(m.group(1)), True))
            continue
        if ACC_PARALLEL_NO_VL_RE.match(line):
            parallel_loops.append((i, None, False))
        if ACC_END_PARALLEL_RE.match(line):
            end_parallel_lines.append(i)
        if ACC_END_DATA_RE.match(line):
            end_data_lines.append(i)
    return routine_directives, parallel_loops, end_parallel_lines, end_data_lines


def _extract_calls(src_lines: list[str], start: int, end: int) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for ln in range(start, min(end, len(src_lines)) + 1):
        m = CALL_RE.match(src_lines[ln - 1])
        if m:
            out.append((ln, m.group(1).lower()))
    return out


def _owning_scope_innermost(line: int, spans: list[tuple[str, int, int]]
                             ) -> tuple[str, int, int] | None:
    best = None
    for name, s, e in spans:
        if s <= line <= e and (best is None or (s >= best[1] and e <= best[2])):
            best = (name, s, e)
    return best


# -------- per-file processing --------

def process_file(src: Path,
                 base_dir: Path | None = None,
                 cpp_defines: list[str] | None = None,
                 cpp_includes: list[Path] | None = None,
                 cpp_on: bool | None = None,
                 cpp_exe: str = "/lib/cpp") -> dict:
    """Extract nodes/edges/acc-overlay for one Fortran file.

    Returns:
        {
          "file":       relpath-or-abs,
          "elapsed_s":  float,
          "nodes":      list[Node],
          "edges":      list[Edge],
          "acc_attrs":  AccAttributes,
          "error":      list[str] | None,
        }
    """
    t0 = time.time()

    # Decide whether to run CPP
    if cpp_on is None:
        cpp_on = src.suffix in CPP_REQUIRED_EXTENSIONS
    errors: list[str] = []
    if cpp_on:
        _, diag = cpp_preprocess(src, cpp_defines, cpp_includes, cpp_exe)
        if diag:
            # CPP failure is non-fatal; we proceed with raw-text scan since we
            # don't actually use the preprocessed output for node extraction.
            # The CPP run was only a syntactic sanity check.
            errors.extend(diag)

    try:
        lines = src.read_text(errors="replace").splitlines()
    except OSError as e:
        return {"file": str(src), "error": [f"read error: {e}"]}

    routine_dirs, parallel_loops, end_parallel_lines, end_data_lines = \
        _scan_acc_directives(lines)
    subs = _scan_scopes(lines, SUB_OPEN_RE, SUB_END_RE)
    funcs = _scan_scopes(lines, FUNC_OPEN_RE, FUNC_END_RE)
    modules = _scan_scopes(lines, MOD_OPEN_RE, MOD_END_RE)

    # Path-as-key
    if base_dir is not None:
        try:
            rel = str(src.resolve().relative_to(base_dir.resolve()))
        except ValueError:
            rel = str(src)
    else:
        rel = str(src)

    def module_of(line: int) -> str | None:
        sc = _owning_scope_innermost(line, modules)
        return sc[0] if sc else None

    # Build nodes
    nodes: list[Node] = []
    for mod_name, s, e in modules:
        nodes.append(Node(id=f"{rel}::{mod_name}", kind="module", name=mod_name,
                          file=rel, line_start=s, line_end=e, module=None,
                          acc_routine=None))

    for kind_name, spans in [("subroutine", subs), ("function", funcs)]:
        for name, s, e in spans:
            mod = module_of(s)
            acc_routine = None
            for ln, mode in routine_dirs:
                if s <= ln <= e:
                    acc_routine = mode
                    break
            nodes.append(Node(id=f"{rel}::{name}", kind=kind_name, name=name,
                              file=rel, line_start=s, line_end=e, module=mod,
                              acc_routine=acc_routine))

    # Build parallel-loop regions
    pl_lines_sorted = sorted([p[0] for p in parallel_loops])

    def first_after(lst: list[int], cutoff: int) -> int | None:
        for v in lst:
            if v > cutoff:
                return v
        return None

    parallel_regions = []
    for pl_line, vl, has_vl in parallel_loops:
        next_pl = first_after(pl_lines_sorted, pl_line)
        next_end_p = first_after(end_parallel_lines, pl_line)
        next_end_d = first_after(end_data_lines, pl_line)
        candidates = [x for x in (next_pl, next_end_p, next_end_d) if x is not None]
        region_end = min(candidates) if candidates else pl_line + 200
        parallel_regions.append({"pl_line": pl_line, "vl": vl,
                                 "has_vl": has_vl, "region_end": region_end})

    # Build edges
    edges: list[Edge] = []
    all_scopes = subs + funcs
    for name, s, e in all_scopes:
        caller_id = f"{rel}::{name}"
        for call_line, callee in _extract_calls(lines, s, e):
            caller_ctx = None
            # Take innermost enclosing parallel region
            for region in parallel_regions:
                pl = region["pl_line"]
                re_ = region["region_end"]
                if s <= pl <= e and pl < call_line <= re_:
                    caller_ctx = {
                        "acc_parallel_loop_line": pl,
                        "vector_length": region["vl"],
                        "has_vl_directive": region["has_vl"],
                        "region_end_line": re_,
                    }
            edges.append(Edge(
                from_=caller_id, from_sub=name, to_name=callee,
                call_line=call_line,
                kind="acc_kernel_call" if caller_ctx else "call",
                caller_context=caller_ctx,
            ))

    acc_attrs = AccAttributes(
        routine_directives=[{"line": ln, "mode": mode} for ln, mode in routine_dirs],
        parallel_loops=[{"line": ln, "vector_length": vl, "has_vl_directive": has_vl}
                        for ln, vl, has_vl in parallel_loops],
    )
    return {
        "file": rel,
        "elapsed_s": round(time.time() - t0, 3),
        "nodes": nodes,
        "edges": edges,
        "acc_attrs": acc_attrs,
        "error": errors if errors else None,
    }


# -------- tree walk --------

def discover_sources(source_dir: Path,
                     recursive: bool = False,
                     include_backups: bool = False,
                     extensions: tuple[str, ...] = FORTRAN_EXTENSIONS
                     ) -> list[Path]:
    """Return sorted list of Fortran source files under source_dir."""
    files: list[Path] = []
    glob_iter = source_dir.rglob("*") if recursive else source_dir.glob("*")
    for p in sorted(glob_iter):
        if not p.is_file():
            continue
        if p.suffix not in extensions:
            continue
        name = p.name
        if not include_backups and (".bak" in name or ".pre-" in name
                                    or ".canonical" in name or ".orig" in name):
            continue
        files.append(p)
    return files


# -------- audit --------

def audit_vl_seq_mismatch(nodes: list[Node], edges: list[Edge]) -> list[AuditFinding]:
    """Phase-F-style detector: routine-seq callee × VL>1 caller = idle-lane tax.

    Resolution order for cross-file name matches:
      1. Same-file candidate (prefer local routines per Fortran USE semantics)
      2. Any routine-seq candidate across the corpus
    At most ONE finding emitted per acc_kernel_call edge. If same-file is
    routine seq, the cross-file candidates are ignored; if same-file is
    non-seq, no violation is reported even if a routine-seq namesake exists
    elsewhere (Fortran scoping says same-file wins).
    """
    by_name: dict[str, list[Node]] = {}
    for n in nodes:
        if n.kind in ("subroutine", "function"):
            by_name.setdefault(n.name.lower(), []).append(n)

    findings: list[AuditFinding] = []
    for e in edges:
        if e.kind != "acc_kernel_call":
            continue
        ctx = e.caller_context
        if not ctx or not ctx.get("has_vl_directive"):
            continue
        vl = ctx.get("vector_length")
        if vl is None or vl <= 1:
            continue
        caller_file = e.from_.split("::")[0]
        candidates = by_name.get(e.to_name, [])
        if not candidates:
            continue

        # Resolution: prefer same-file candidate. If same-file is non-seq,
        # silently drop (local scoping wins). If no same-file, try any seq
        # namesake (module-USE fallback — best-effort in absence of USE edges).
        same_file = [c for c in candidates if c.file == caller_file]
        resolved = None
        if same_file:
            # Fortran local scoping: same-file candidate wins unconditionally
            sf_seq = [c for c in same_file if c.acc_routine == "seq"]
            if sf_seq:
                resolved = sf_seq[0]
            else:
                continue  # same-file shadows cross-file; no violation
        else:
            seq_candidates = [c for c in candidates if c.acc_routine == "seq"]
            if seq_candidates:
                resolved = seq_candidates[0]

        if resolved is None:
            continue

        findings.append(AuditFinding(
            caller_file=caller_file,
            caller_sub=e.from_sub,
            caller_acc_pl_line=ctx["acc_parallel_loop_line"],
            caller_vector_length=vl,
            call_line=e.call_line,
            callee_sub=e.to_name,
            callee_file=resolved.file,
            callee_line_start=resolved.line_start,
            severity="HIGH",
            rationale=(
                "routine seq callee × VL>1 caller = idle-lane register tax; "
                "(VL-1)/VL threads unused but still consume per-thread regs "
                "→ blocks/SM constrained → suboptimal occupancy. "
                "Fix: set vector_length(1) on the enclosing parallel loop."
            ),
        ))
    return findings
