"""fortran-acc-audit — static audit for OpenACC directive patterns in Fortran.

Designed for NWP/climate GPU porting efforts (WRF, CESM, MPAS, ICON, etc.)
that share the column-sequential `!$acc routine seq` + `!$acc parallel loop
gang vector_length(N)` idiom. Detects idle-lane-tax patterns (seq callee
under VL>1 caller) that are invisible to occupancy-centric tuning but
directly degrade GPU utilization.
"""
__version__ = "0.1.0"
__all__ = ["extractor", "cli", "schema"]
