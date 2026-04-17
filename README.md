# fortran-acc-audit

[![CI](https://github.com/gonos2k/fortran-acc-audit/actions/workflows/ci.yml/badge.svg)](https://github.com/gonos2k/fortran-acc-audit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

Static audit for OpenACC directive patterns in Fortran. Built for NWP and climate GPU porting efforts (WRF, CESM, MPAS, ICON, E3SM, IFS, KIM-meso, …) that share the column-sequential `!$acc routine seq` + `!$acc parallel loop gang vector_length(N)` idiom.

Detects the **idle-lane-tax pattern** where a `routine seq` callee is reached from a `parallel loop` with `vector_length > 1`. In that configuration, NVHPC allocates full register footprint for all N lanes per gang but only 1 lane executes — the other N-1 are pure register tax, collapsing blocks/SM and crippling GPU occupancy. A 2-line `vector_length(128) → (1)` fix in the KIM-meso port produced a 32% wall-time reduction; this tool makes the pattern mechanically detectable on any similar codebase before it burns dev-days.

## Install

```bash
pip install fortran-acc-audit        # from PyPI (when released)
pip install -e .                     # from source checkout
```

Python 3.10+, no runtime dependencies. Optional `fparser2` for future fparser-backed scope detection:

```bash
pip install fortran-acc-audit[fparser]
```

## Quick start

```bash
fortran-acc-audit --source-dir path/to/phys/ --out-dir audit_out/
```

Outputs to `audit_out/`:

| file | purpose |
|---|---|
| `nodes.json` | Subroutines, functions, modules |
| `edges.json` | CALL edges + OpenACC kernel context |
| `acc_attributes.json` | Per-file directive overlay |
| `audit_vl_seq_mismatch.json` | **Violations (the whole point)** |
| `manifest.json` | Scan record + per-file timing |

Exit 0 on success (audit may be empty or non-empty). Add `--fail-on-violation` to exit 1 when any violation found — good for CI.

### CI guard example

```yaml
# .github/workflows/openacc-audit.yml
- name: Fortran OpenACC audit
  run: |
    pip install fortran-acc-audit
    fortran-acc-audit --source-dir phys/ --fail-on-violation
```

### With preprocessor defines (WRF-style)

```bash
fortran-acc-audit --source-dir phys/ \
  -D EM_CORE=1 -D _GPU \
  -I ../inc \
  --recursive
```

## What it detects

### Primary: routine-seq × VL>1 mismatch

```fortran
!$acc parallel loop gang vector_length(128)   ! ← caller VL
do i = 1, n
  call column_physics(x(i))                    ! ← call inside region
end do

subroutine column_physics(a)
  !$acc routine seq                            ! ← callee declared seq
  ...
end subroutine
```

This is pessimal on GPU: only lane 0 does work per gang, other 127 sit idle holding register allocations. On NVIDIA cc89 (65,536 regs/SM, 255-reg cap per thread), blocks/SM drops from 24 (at VL=1) to 2 (at VL=128), reducing active threads GPU-wide from ~576 to ~48 — a 12× latency-hiding collapse with no useful work tradeoff.

Fix: change to `vector_length(1)`. See [the Phase F writeup](#background) for the full derivation.

### Current scope (v0.1)

- Node extraction: `SUBROUTINE`, `FUNCTION`, `MODULE` (with line spans)
- Edge extraction: `CALL` statements, with OpenACC kernel context when inside a `!$acc parallel loop` region
- Directive overlay: `!$acc routine seq|worker|vector`, `!$acc parallel loop ... vector_length(N)`
- Region boundary detection: `!$acc end parallel`, `!$acc end data`, next `!$acc parallel loop` (first-wins)
- Cross-file name resolution: same-file priority (Fortran local-scope semantics)

Not yet covered (planned for v0.2+):
- `USE` module-dependency edges
- `!$acc data` region nodes (data-flow audit)
- `!$acc kernels`, `!$acc loop`, `!$acc update`, `!$acc enter/exit data`
- fparser2-backed scope detection (current: raw-text regex; accurate for flat modules, approximate for deeply nested CONTAINS)

Not planned:
- OpenMP target directives
- CUDA Fortran attribute extraction
- `INCLUDE` chain resolution (WRF `HALO_EM_*.inc`)

## Integration with knowledge graphs

`--emit-graphify-layer PATH` emits a secondary JSON in NetworkX `node_link_data` format, mergeable with graphify-produced graphs or any NetworkX consumer:

```bash
fortran-acc-audit --source-dir phys/ --out-dir audit_out/ \
  --emit-graphify-layer audit_out/fortran_layer.json
```

```python
import json
from networkx.readwrite import json_graph
data = json.load(open("audit_out/fortran_layer.json"))
G = json_graph.node_link_graph(data, edges="links")
```

This is a pure JSON serialization — no runtime dependency on any graph tool.

## Supported extensions

`.F`, `.F90`, `.F95`, `.F03`, `.F08`, `.f`, `.f90`, `.f95`, `.f03`, `.f08`.

Uppercase extensions trigger an optional CPP preprocessing pass (syntactic sanity check only — the raw source is still used for extraction). Disable with `--no-cpp`.

## CLI reference

```
fortran-acc-audit --source-dir DIR [options]

Required:
  --source-dir DIR           Fortran source root.

Output:
  --out-dir DIR              Output JSON directory. Default: acc_audit_output/
  --emit-graphify-layer PATH Secondary NetworkX node_link_data JSON (opt-in).

Source discovery:
  --recursive                Recurse into subdirectories.
  --include-backups          Include .bak_*, .pre-*, .canonical, .orig files.
  --extensions EXT [EXT ...] Limit extensions (default: all standard Fortran).

Preprocessing:
  -D MACRO[=VAL]             CPP define (repeatable).
  -I PATH                    CPP include dir (repeatable).
  --no-cpp                   Skip CPP for .F/.F90.
  --cpp-exe PATH             CPP executable (default: /lib/cpp).

Exit policy:
  --fail-on-violation        Exit 1 if audit finds any violation.

Other:
  --quiet                    Suppress per-file progress.
  --version
```

## Background

This tool extracts a pattern first identified in the KIM-meso GPU port:

- **Before** (2026-04-16): `!$acc parallel loop gang vector_length(128)` calling `kdm62D_column` (routine seq). Median step = 30.10 s, real-time ratio = 1.1×.
- **After** (2026-04-17): `vector_length(128) → (1)` (2-line change). Median step = 20.42 s (-32.2%), real-time ratio = 2.0×. Zero numerical regression (D3 paired-run validated: t=0 bit-identical, t=1 bounded FP32 drift).

The pattern is plausibly present across many NWP/climate ports because they share the same column-sequential physics structure. This tool exists so the next team doesn't spend 2 dev-days on maxregcount / routine-split / anti-inline tuning before discovering that their actual bottleneck is thread utilization, not register allocation.

## Status

**v0.1 — alpha**. Core pattern detection validated on KIM-meso (463 files, 6954 nodes, 16102 edges, 3.6 s scan). Fixture-based tests cover positive/negative/host-only cases. API may evolve.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Issues and PRs welcome, especially:
- Test fixtures from other NWP/climate codebases
- False-positive or false-negative cases from real ports
- OpenACC directive coverage extensions
- fparser2 production path for nested-scope accuracy

See [CONTRIBUTING.md](CONTRIBUTING.md) for details.
