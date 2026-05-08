"""click_plc -- read/write AutomationDirect CLICK PLC project files (.ckp).

Reverse-engineered from the EB Pro / CLICK Programming Software v3.43 binary.

Public API::

    from click_plc import decode_ckp, compute_magic, Rung
    from click_plc.ckp_decoder import CkpProject

This package is a backend for the `universal_machinery` PLC toolkit
(https://github.com/iliana/universal_machinery) but works standalone too.
"""
from .ckp_decoder import (
    CkpProject,
    Instruction,
    NickEntry,
    Rung,
    Subroutine,
    compute_magic,
    decode_ckp,
)

__all__ = [
    "CkpProject",
    "Instruction",
    "NickEntry",
    "Rung",
    "Subroutine",
    "compute_magic",
    "decode_ckp",
]

__version__ = "0.1.0"
