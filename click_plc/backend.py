"""CLICK backend: read / write AutomationDirect ``.ckp`` project files.

Wires the package's existing CKP decoder + the parent project's
IL → CLICK lowering into the ``universal_machinery.backends.Backend``
ABC so callers can target CLICK PLCs the same way they'd target
OpenPLC or rusty.

Status: SCAFFOLD.  The package's ``decode_ckp`` produces a vendor-
specific ``CkpProject`` AST, not a ``universal_machinery.il.Program``.
The parent project ships an IL → CLICK calling-convention lowering
at ``universal_machinery.lowering.click_calling``, but the encoder
side (``CkpProject`` -> ``.ckp`` bytes, plus the
``CkpProject`` <-> IL bridge) doesn't exist yet -- both directions
of ``ClickBackend`` raise ``NotImplementedError`` with a pointer to
the roadmap.

Roadmap (per parent ``docs/ROADMAP.md``):

  - Settle the IL ↔ CLICK lowering so ``Program`` can round-trip
    through ``.ckp``.  The structural Backend ABC registration here
    is step 1; the encoder + CkpProject↔Program bridge follow.

Once the bridge lands ``ClickBackend.write(program, "out.ckp")``
will:

  1. Run the parent's ``lowering.click_calling`` pass to convert
     the IL into CLICK-native call shapes.
  2. Translate the lowered ``Program`` into a ``CkpProject`` via a
     new bridge module.
  3. Encode the ``CkpProject`` to ``.ckp`` bytes via a new encoder
     pass (the decoder's inverse).

``ClickBackend.read("in.ckp")`` will do the inverse: decode bytes
-> ``CkpProject`` -> IL ``Program``.
"""

from __future__ import annotations

from pathlib import Path

from universal_machinery.backends import Backend, register
from universal_machinery.il import Program


@register("click")
class ClickBackend(Backend):
    """Read / write CLICK ``.ckp`` project files via the IL.

    Output format: ``.ckp`` (AutomationDirect CLICK Programming
    Software binary project file).

    Capability set reflects what the IL → CLICK lowering pass
    (``universal_machinery.lowering.click_calling``) covers today.
    The encoder side that finishes the lowering and writes the
    actual ``.ckp`` bytes is the pending roadmap item; until it
    lands, ``write()`` and ``read()`` raise ``NotImplementedError``
    with pointers to the parent project's roadmap.
    """

    name = "click"
    #: Capabilities the IL → CLICK lowering supports today.  The
    #: lowering pass at ``universal_machinery.lowering.click_calling``
    #: covers all of the CLICK PLC's ladder surface plus the calling
    #: convention's parameter marshalling.  Notably absent: ``sfc``
    #: (CLICK has no SFC), ``functions`` (CLICK's FUNCTION shape
    #: isn't IEC-aligned), ``methods`` / ``interfaces`` / ``extends``
    #: / ``implements`` / ``abstract`` (CLICK has no OOP), ``st``
    #: (CLICK has limited free-form statements, not IEC §3 ST).
    capabilities = frozenset({
        "ld",
        "timers",
        "counters",
        "compare",
        "math",
        "call",
        "function_blocks",
        "jump",
        "parallel",
        "data_blocks",   # CLICK uses contiguous DS regions; the
                          # lowering pass handles the layout.
    })

    def write(self, program: Program, path: str) -> None:
        """Lower ``program`` and write a CLICK ``.ckp`` file.

        SCAFFOLD: not yet implemented.  Needs the
        ``CkpProject`` <-> IL bridge + the ``.ckp`` encoder (the
        decoder's inverse).  Both are tracked under the parent
        project's roadmap item *"Settle the IL ↔ CLICK lowering
        so Program can round-trip through .ckp"*.
        """
        raise NotImplementedError(
            "ClickBackend.write: .ckp encoder not yet implemented.  "
            "The parent project ships an IL → CLICK calling-convention "
            "lowering at ``universal_machinery.lowering.click_calling``, "
            "but the encoder side that turns the lowered Program into "
            ".ckp bytes is a pending roadmap item.  Track in "
            "``docs/ROADMAP.md``."
        )

    def read(self, path: str) -> Program:
        """Decode a ``.ckp`` file and return an IL ``Program``.

        Reads the bytes via ``decode_ckp`` (vendor-native AST) then
        translates to the IL via
        :func:`click_plc.ckp_to_il.ckp_to_il`.

        Op coverage in this first slice: the simple LD ops with
        direct IL analogues -- ContactNO / ContactNC, OutCoil /
        OutSet / OutReset, Copy (-> Move), Call / Return / End.
        Multi-operand ops (Compare / Math / Tmr / Edge / For-Next)
        are skipped from rungs silently; the surrounding contacts
        and coils still survive so the program structure is
        usable for inspection / diff workflows.  Future slices
        will fill in the gaps.

        For byte-level CKP introspection without IL translation,
        use ``click_plc.decode_ckp(bytes)`` directly.
        """
        from .ckp_decoder import decode_ckp
        from .ckp_to_il import ckp_to_il

        with open(path, "rb") as f:
            data = f.read()
        return ckp_to_il(decode_ckp(data))
