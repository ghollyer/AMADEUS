"""AMADEUS setup wizard and microscope-PC launcher (Tk, stdlib only).

    python -m setup_wizard              # setup wizard (either machine)
    python -m setup_wizard.launcher     # service launcher (microscope PC)

Must stay Python 3.10-compatible and import only ``common/`` plus the stdlib,
so it runs under the vjem310 interpreter on the microscope PC.
"""
