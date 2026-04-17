# Contributing to fortran-acc-audit

Thanks for considering a contribution. This project targets a niche (NWP/climate Fortran + OpenACC), so domain-specific fixtures and edge cases from real codebases are especially valuable.

## Development setup

```bash
git clone https://github.com/gonos2k/fortran-acc-audit.git
cd fortran-acc-audit
pip install -e .[dev]
pytest -v
```

## Running the audit against your codebase

```bash
fortran-acc-audit --source-dir /path/to/your/fortran/ --out-dir /tmp/out
```

If the tool produces unexpected results on your source, please open an issue with:
- A minimal (or anonymized) reproducer file
- The expected audit verdict
- The output produced

## What contributions are most welcome

**High value**:
- Test fixtures from NWP/climate codebases (WRF, CESM, ICON, MPAS, IFS, E3SM, …)
- False-positive reports (the detector claimed a violation that isn't)
- False-negative reports (a known bad pattern the detector missed)
- OpenACC directive coverage additions (e.g. `!$acc kernels` handling)
- fparser2-backed scope detection for nested CONTAINS accuracy

**Medium value**:
- Performance optimizations (current: ~4s on 463 files; acceptable but could be better)
- Additional output formats (DOT, GraphML, etc.)
- CI recipe examples for other build systems

**Out of scope for now**:
- OpenMP target directive support (see [README](README.md) — considered for future)
- CUDA Fortran attribute extraction (ditto)
- Full Fortran parse tree (this is an audit tool, not a compiler)

## Style

- Python 3.10+, use type hints
- `ruff format` + `ruff check`
- Tests: pytest, fixture-based, no live network
- Keep dependencies minimal (stdlib only for core extraction)

## Commits

Conventional-ish: short subject in imperative, body explaining the why if non-trivial. No strict format.

## Reviews

Maintainer reviews on a best-effort basis (this is a side project). Expect a few days turnaround.
