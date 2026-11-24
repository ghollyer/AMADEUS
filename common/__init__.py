"""Shared protocol definitions for AMADEUS processes.

Imported by the relay, the drivers, the setup tools, the
client, and tests.
Everything here must stay compatible with Python 3.10 (the vjem310 conda env)
and depend on nothing beyond the standard library.

``dm_server`` deliberately does *not* import from here: it is instrument-tested
code exec'd inside Digital Micrograph and stays self-contained. Its
``server.py`` is the reference implementation of the wire protocol; if the
protocol ever changes, change both.
"""
