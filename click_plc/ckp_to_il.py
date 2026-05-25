"""``CkpProject`` -> ``universal_machinery.il.Program`` adapter.

Bridges the vendor-native ``CkpProject`` AST (produced by
``decode_ckp``) into the IL ``Program`` shape so callers can:

  bytes (.ckp) -> decode_ckp -> CkpProject -> ckp_to_il -> IL Program

``ClickBackend.read`` uses this on the read direction; the write
direction (encoder) is a separate pending slice.

Scope (first slice)
-------------------

Conversion of the simple ladder ops that have direct IL analogues:

  - Tags: ``CkpProject.nicknames`` -> ``Program.tags`` keyed by
    nickname (when present) or by raw address.
  - Subroutines: ``CkpProject.subroutines`` -> ``Program.subroutines``.
    The first subroutine is marked ``main=True`` (CLICK's convention --
    sub_id 1 is the cyclically-scheduled main program); the rest are
    callable subroutines (still ``PouKind.PROGRAM`` since CLICK doesn't
    model the FUNCTION / FUNCTION_BLOCK split).
  - Rungs: each vendor rung becomes an IL ``Rung`` carrying the
    recognised ops.  Recognised opcodes:

      ======  ===========  ===========================
      0x11    ContactNO    -> ``il.ContactNO(Address)``
      0x12    ContactNC    -> ``il.ContactNC(Address)``
      0x15    Out          -> ``il.OutCoil(Address)``
      0x16    OutSet       -> ``il.OutSet(Address)``
      0x17    OutReset     -> ``il.OutReset(Address)``
      0x21    Copy         -> ``il.Move(src, dst)``
      0x23    Call         -> ``il.Call(target=name)``
      0x24    Return       -> ``il.Return()``
      0x27    End          -> ``il.End()``
      ======  ===========  ===========================

  Unrecognised opcodes (Edge / Compare / Math / Tmr / For / Next)
  are skipped from the rung silently -- a later slice maps them to
  their multi-operand IL equivalents.  The structural shape (POU
  set, rung count) survives in either case so the read direction
  is useful even with partial op coverage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from universal_machinery.il import (
    Address, Program, PouKind, Subroutine, Tag, TagType,
)
from universal_machinery.il.ast import Rung
from universal_machinery.il.ops import (
    Call, ContactNC, ContactNO, End, Move, OutCoil, OutReset, OutSet,
    Return,
)

if TYPE_CHECKING:
    from .ckp_decoder import CkpProject, Instruction
    from .ckp_decoder import Subroutine as CkpSubroutine


def ckp_to_il(project: "CkpProject") -> Program:
    """Translate a ``CkpProject`` into a ``universal_machinery`` IL
    ``Program``.

    See module docstring for the supported-op scope.  Unsupported
    opcodes are skipped from rungs rather than raising so a
    real-world ``.ckp`` (which mixes simple LD with timers / math /
    compares) still produces a structurally usable IL program for
    inspection + diff workflows.
    """
    return Program(
        subroutines=[
            _convert_subroutine(sub, is_main=(idx == 0))
            for idx, sub in enumerate(project.subroutines)
        ],
        tags=_nicknames_to_tags(project),
    )


def _nicknames_to_tags(project: "CkpProject") -> dict[str, Tag]:
    """Build the IL ``Program.tags`` table from
    ``CkpProject.nicknames``.

    Each ``NickEntry`` becomes a ``Tag``:

      - ``name``: the nickname when non-empty, otherwise the raw
        address (so callers always have a stable lookup key).
      - ``address``: the raw CLICK address (e.g. ``X001``) wrapped
        in an ``Address``.
      - ``description``: the entry's ``default`` field (CLICK uses
        it for the I/O point's "default tagname" comment).

    Duplicate names get an integer suffix so the dict stays
    one-to-one with the source entries -- the CLICK editor allows
    addresses with no nickname, so multiple unnamed entries would
    otherwise collide on the address fallback key.
    """
    tags: dict[str, Tag] = {}
    for entry in project.nicknames:
        name = entry.nickname or entry.address
        # Avoid silently overwriting on collisions.
        unique = name
        n = 1
        while unique in tags:
            n += 1
            unique = f"{name}#{n}"
        tags[unique] = Tag(
            name=unique,
            data_type=_address_to_tagtype(entry.address),
            address=Address(raw=entry.address),
            description=entry.default,
        )
    return tags


def _address_to_tagtype(raw: str) -> TagType:
    """Guess a CLICK address's IL data type from its prefix.

    CLICK's address space conventions (see
    https://library.automationdirect.com/click-plus-programming/ ):

      ============  ====================================  ========
      Prefix        Meaning                               TagType
      ============  ====================================  ========
      X / Y / C     Discrete I/O / control bits           BOOL
      T / CT        Timer / counter "done" bits           BOOL
      SC           System control bits                   BOOL
      DS           Data storage (signed 16-bit)          INT
      DD           Data storage (signed 32-bit)          DINT
      DF           Data storage (32-bit float)           REAL
      DH           Data storage (hex 16-bit)             WORD
      TD / CTD     Timer / counter accumulators (DINT)   DINT
      TXT          Text storage                          STRING
      ============  ====================================  ========

    Unknown / unrecognised prefix defaults to ``BOOL`` -- safer
    fallback than INT since the address space is dominated by
    discrete I/O in typical CLICK projects.
    """
    if not raw:
        return TagType.BOOL
    head = raw.rstrip("0123456789")
    return {
        "X":   TagType.BOOL,
        "Y":   TagType.BOOL,
        "C":   TagType.BOOL,
        "T":   TagType.BOOL,
        "CT":  TagType.BOOL,
        "SC":  TagType.BOOL,
        "DS":  TagType.INT,
        "DD":  TagType.DINT,
        "DF":  TagType.REAL,
        "DH":  TagType.WORD,
        "TD":  TagType.DINT,
        "CTD": TagType.DINT,
        "TXT": TagType.STRING,
    }.get(head, TagType.BOOL)


def _convert_subroutine(sub: "CkpSubroutine", *, is_main: bool) -> Subroutine:
    """Translate one vendor ``Subroutine`` to an IL ``Subroutine``.

    All CKP subroutines map to ``PouKind.PROGRAM``: CLICK doesn't
    model the IEC FUNCTION / FUNCTION_BLOCK distinction, and the
    first subroutine (sub_id 1) is the cyclically-scheduled main
    program (``main=True``); the rest are callable units invoked
    via the ``Call`` op.
    """
    return Subroutine(
        name=sub.name,
        kind=PouKind.PROGRAM,
        main=is_main,
        rungs=[_convert_rung(rung) for rung in sub.rungs],
    )


def _convert_rung(instrs: "list[Instruction]") -> Rung:
    """Translate one vendor rung (a list of ``Instruction``) to an
    IL ``Rung`` carrying only the ops this slice recognises.

    Unrecognised ops are dropped silently; the empty-rung case is
    valid IL (matches what a vendor "blank rung" looks like).
    """
    ops: list[object] = []
    for instr in instrs:
        il_op = _convert_instruction(instr)
        if il_op is not None:
            ops.append(il_op)
    return Rung(ops=tuple(ops))


def _convert_instruction(instr: "Instruction"):
    """Map one vendor ``Instruction`` to an IL op, or ``None`` if
    its opcode isn't in this slice's scope.

    Multi-operand ops (Compare 0x14, Math 0x1a) carry their
    operands in ``Instruction.operands``; this slice doesn't model
    them yet, so they return None (the rung still gets the
    surrounding contacts / coils).  Future slices will fill in:

      - 0x14 Compare -> il.Compare(op=..., lhs=..., rhs=...)
      - 0x1a Math    -> il.BinaryMath(op=..., lhs=..., rhs=..., dst=...)
      - 0x18 Tmr     -> il.TON(...) with timer config bytes parsed out
      - 0x13 Edge    -> il.ContactRisingEdge / ContactFallingEdge
      - 0x25 For / 0x26 Next -> no direct IEC equivalent (vendor-only
        construct; will need a lowering pass to IL FOR loops)
    """
    opcode = instr.opcode
    tag = instr.tag

    # Simple ops carrying a single address operand.
    if opcode == 0x11 and tag:
        return ContactNO(Address(raw=tag))
    if opcode == 0x12 and tag:
        return ContactNC(Address(raw=tag))
    if opcode == 0x15 and tag:
        return OutCoil(Address(raw=tag))
    if opcode == 0x16 and tag:
        return OutSet(Address(raw=tag))
    if opcode == 0x17 and tag:
        return OutReset(Address(raw=tag))

    # Two-operand Copy (0x21) -- ``operands`` is [src, dst].
    if opcode == 0x21 and len(instr.operands) >= 2:
        src, dst = instr.operands[0], instr.operands[1]
        return Move(src=Address(raw=src), dst=Address(raw=dst))

    # Control flow.
    if opcode == 0x23 and tag:
        return Call(target=tag)
    if opcode == 0x24:
        return Return()
    if opcode == 0x27:
        return End()

    # Unhandled: 0x13 Edge / 0x14 Compare / 0x18 Tmr / 0x1a Math /
    # 0x25 For / 0x26 Next.  Future slice.
    return None
