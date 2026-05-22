"""click_plc -- read/write AutomationDirect CLICK PLC project files (.ckp).

Reverse-engineered from the EB Pro / CLICK Programming Software v3.43 binary.

Public API::

    # Low-level CKP I/O (vendor-native ``CkpProject`` AST):
    from click_plc import decode_ckp, compute_magic, Rung
    from click_plc.ckp_decoder import CkpProject

    # Backend ABC integration with universal_machinery (scaffold;
    # write/read currently raise NotImplementedError -- the encoder
    # + CkpProject↔Program bridge are pending roadmap items):
    from click_plc import ClickBackend
    # Or via the parent registry after importing this package:
    import click_plc
    from universal_machinery.backends import get_backend
    backend = get_backend("click")

This package is a backend for the `universal_machinery` PLC toolkit
(https://github.com/iliana/universal_machinery).  The vendor-native
``CkpProject`` API still works standalone for byte-level CKP
introspection; the ``ClickBackend`` integration requires the parent
``universal_machinery`` package installed.
"""
from .backend import ClickBackend
from .ckp_decoder import (
    CkpProject,
    Instruction,
    NickEntry,
    Rung,
    Subroutine,
    compute_magic,
    decode_ckp,
)
from .hardware import (
    CATALOG,
    HardwareConfig,
    SlotInfo,
    describe_hardware,
    lookup_module,
)

__all__ = [
    "CATALOG",
    "CkpProject",
    "ClickBackend",
    "HardwareConfig",
    "Instruction",
    "NickEntry",
    "Rung",
    "SlotInfo",
    "Subroutine",
    "compute_magic",
    "decode_ckp",
    "describe_hardware",
    "lookup_module",
]

__version__ = "0.1.0"
