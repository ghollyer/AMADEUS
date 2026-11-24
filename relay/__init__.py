"""The AMADEUS server: the single instrument-side ZMQ endpoint.

Runs on the microscope PC (in the vjem310 env on a JEOL: Python 3.10 +
pyzmq). The MCP client connects here. Each command goes to the driver holding
its slot (``common/commands.py``, ``relay/routing.py``): drivers run in this
process, in their own supervised processes, or at an address (Digital
Micrograph). The drivers' live frames are republished on the public stream
port.

    python -m relay                            # everything from amadeus_config.json
    python -m relay --offline | --online | --sim
"""
