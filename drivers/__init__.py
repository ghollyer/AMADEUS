"""Vendor drivers: one per microscope subsystem implementation.

``base`` defines the slot interfaces, ``dispatch`` runs requests against a
driver, ``registry`` builds drivers by kind, ``host`` serves one driver in its
own process, ``dm`` adapts Digital Micrograph, ``sim`` simulates every slot,
``contract`` checks a driver against the interfaces, ``smoke`` is the
read-only first check on a real instrument and ``template`` is the starting
point for a new vendor. See docs/ADDING_HARDWARE.md.

Everything here is stdlib (+ pyzmq for the host) and Python 3.10-compatible:
drivers run in vendor Python environments such as JEOL's vjem310.
"""
