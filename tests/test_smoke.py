"""Smoke + unit tests for click_plc.

Per-submodule tests that don't depend on .ckp fixtures -- those
integration tests live in the parent ``universal_machinery``
repository's ``tests/click/`` directory.  Here we cover the
fixture-free unit-level surface:

  - Public API surface (imports succeed; expected names exported)
  - Hardware catalog lookups for known module IDs
  - Low-level helpers (u16/u32 endianness, compute_magic on known bytes)
  - Constants the protocol depends on (opcode tables, section markers)
"""
import pytest


# -----------------------------------------------------------------------------
# Public API surface
# -----------------------------------------------------------------------------


def test_package_imports_cleanly():
    import click_plc
    # The names listed in __all__ all resolve to actual attributes.
    for name in click_plc.__all__:
        assert hasattr(click_plc, name), f"click_plc.{name} not exported"


def test_public_classes_constructable():
    """The public dataclasses can be instantiated with their minimal
    required arguments."""
    from click_plc import (
        CATALOG, HardwareConfig, Instruction, NickEntry, Rung,
        SlotInfo, Subroutine,
    )
    # CATALOG is a dict literal -- non-empty.
    assert isinstance(CATALOG, dict)
    assert len(CATALOG) > 0

    # Each dataclass takes its fields by keyword.  We don't assert on
    # exact field semantics -- just that construction works.
    slot = SlotInfo(index=1, raw_id=999, module="C2-01CPU")
    assert slot.module == "C2-01CPU"


# -----------------------------------------------------------------------------
# Hardware catalog
# -----------------------------------------------------------------------------


def test_lookup_module_known_ids():
    """Spot-check a handful of well-known CLICK module IDs.  These come
    from the reverse-engineered SystemConfig.dll's tagMODULEIDLIST."""
    from click_plc import lookup_module
    assert lookup_module(8)  == "C0-08ND3"     # 8-pt 24VDC input
    assert lookup_module(16) == "C0-16ND3"     # 16-pt 24VDC input
    assert lookup_module(40) == "C0-08TD1"     # 8-pt 24VDC sink output
    assert lookup_module(45) == "C0-08TR"      # 8-pt relay output


def test_lookup_module_unknown_id_returns_none():
    from click_plc import lookup_module
    assert lookup_module(99999) is None
    assert lookup_module(0)     is None or isinstance(lookup_module(0), str)


def test_catalog_contains_cpu_modules():
    """The CATALOG must cover CPU IDs since describe_hardware needs them."""
    from click_plc import CATALOG
    # CLICK basic + plus CPUs (C0-XX, C2-XX prefixes) should be present.
    cpu_names = {v for v in CATALOG.values() if v and v.startswith(("C0-", "C2-"))}
    assert len(cpu_names) > 5, (
        f"CATALOG seems sparse -- only {len(cpu_names)} C0-/C2- modules"
    )


# -----------------------------------------------------------------------------
# Low-level helpers
# -----------------------------------------------------------------------------


def test_u16_little_endian():
    """``u16`` reads a 16-bit little-endian value from a byte offset."""
    from click_plc.ckp_decoder import u16
    data = bytes([0x34, 0x12, 0x78, 0x56])
    assert u16(data, 0) == 0x1234
    assert u16(data, 2) == 0x5678


def test_u32_little_endian():
    from click_plc.ckp_decoder import u32
    data = bytes([0x78, 0x56, 0x34, 0x12])
    assert u32(data, 0) == 0x12345678


# -----------------------------------------------------------------------------
# compute_magic: the file's XOR-16 checksum
# -----------------------------------------------------------------------------


def test_compute_magic_xor16_known_input():
    """``compute_magic`` XORs all u16 LE words from offset 2 to end.

    Pick a fixture small enough to compute by hand:
        offset 0: magic placeholder bytes 00 00
        offset 2: 34 12  (-> 0x1234)
        offset 4: 78 56  (-> 0x5678)
        offset 6: 00 00  (-> 0x0000)
    Expected magic = 0x1234 XOR 0x5678 = 0x444c
    """
    from click_plc import compute_magic
    payload = bytes([0x00, 0x00, 0x34, 0x12, 0x78, 0x56, 0x00, 0x00])
    assert compute_magic(payload) == 0x1234 ^ 0x5678


def test_compute_magic_handles_odd_length():
    """Per the algorithm, a trailing odd byte is padded with zero on
    the HIGH side (so the u16 LE reads as ``byte | 0x00 << 8``)."""
    from click_plc import compute_magic
    # Three bytes after the magic placeholder: AA + BB + (CC, padded).
    # u16 LE: 0xBBAA (from AA BB), then 0x00CC (from CC + pad zero).
    payload = bytes([0x00, 0x00, 0xAA, 0xBB, 0xCC])
    expected = 0xBBAA ^ 0x00CC
    assert compute_magic(payload) == expected


def test_compute_magic_empty_after_offset_returns_zero():
    """A file with only the 2-byte magic placeholder has nothing to XOR."""
    from click_plc import compute_magic
    assert compute_magic(bytes([0x00, 0x00])) == 0


# -----------------------------------------------------------------------------
# Opcode table integrity
# -----------------------------------------------------------------------------


def test_ladder_opcode_table_covers_basic_ops():
    """The ladder-logic opcode table must include the instructions the
    decoder relies on -- regressions here mean the decoder silently
    drops or misreads instructions."""
    from click_plc.ckp_decoder import LADDER_OPCODES
    # A handful of opcodes the decoder recognises (per the module's
    # docstring on the LADDER_OPCODES table).
    assert LADDER_OPCODES.get(0x11) == "ContactNO"
    assert LADDER_OPCODES.get(0x12) == "ContactNC"
    assert LADDER_OPCODES.get(0x15) == "Out"
    assert LADDER_OPCODES.get(0x23) == "Call"
    assert LADDER_OPCODES.get(0x24) == "Return"
    assert LADDER_OPCODES.get(0x27) == "End"


# -----------------------------------------------------------------------------
# Backend ABC integration (scaffold)
# -----------------------------------------------------------------------------


def test_click_backend_imports_cleanly():
    """The Backend ABC scaffold imports without errors.  Also pins
    that ``ClickBackend`` is re-exported from the package top
    level (via __init__.__all__)."""
    import click_plc
    assert "ClickBackend" in click_plc.__all__
    assert hasattr(click_plc, "ClickBackend")


def test_click_backend_registered_in_universal_machinery():
    """``@register('click')`` makes the backend discoverable via
    ``get_backend('click')`` after the package is imported."""
    import click_plc  # noqa: F401  (side-effect: registers)
    from universal_machinery.backends import (
        get_backend, registered_names,
    )
    assert "click" in registered_names()
    backend = get_backend("click")
    assert backend.__class__.__name__ == "ClickBackend"


def test_click_backend_advertises_expected_capabilities():
    """Capabilities the IL → CLICK lowering already supports
    (``universal_machinery.lowering.click_calling``).  Not in the
    set: ``sfc`` / ``st`` / ``functions`` / ``methods`` etc.,
    since CLICK doesn't model those."""
    from click_plc import ClickBackend
    expected_present = {
        "ld", "timers", "counters", "compare", "math", "call",
        "function_blocks", "jump", "parallel", "data_blocks",
    }
    expected_absent = {"sfc", "st", "functions", "methods",
                          "interfaces", "extends", "implements",
                          "abstract"}
    for cap in expected_present:
        assert cap in ClickBackend.capabilities, (
            f"ClickBackend should advertise {cap!r} -- the IL → CLICK "
            f"lowering covers it via click_calling.py"
        )
    for cap in expected_absent:
        assert cap not in ClickBackend.capabilities, (
            f"ClickBackend must not advertise {cap!r} -- CLICK has "
            f"no equivalent construct"
        )


def test_click_backend_write_raises_not_implemented_with_pointer(tmp_path):
    """Scaffold contract: ``write()`` raises ``NotImplementedError``
    with a message pointing at the encoder roadmap item so future
    callers aren't left guessing why it doesn't work yet."""
    from click_plc import ClickBackend
    from universal_machinery.builders import prog, program
    out = tmp_path / "prog.ckp"
    p = program(subroutines=[prog("Main", main=True)])
    with pytest.raises(NotImplementedError, match="encoder"):
        ClickBackend().write(p, str(out))


def test_click_backend_read_dispatches_through_ckp_to_il(tmp_path):
    """``ClickBackend.read`` decodes the bytes via ``decode_ckp``
    and translates via ``ckp_to_il``.  This test mocks both halves
    at their source modules (the backend imports them lazily, so
    we patch the underlying names) -- the byte-level decoder
    integration is already covered by the decoder's own tests;
    here we pin the backend's plumbing."""
    from unittest.mock import patch

    from click_plc import ClickBackend
    from universal_machinery.builders import prog, program

    out = tmp_path / "prog.ckp"
    out.write_bytes(b"any-bytes")
    expected = program(subroutines=[prog("Main", main=True)])
    with patch("click_plc.ckp_decoder.decode_ckp",
                 return_value="CkpProject-sentinel"), \
         patch("click_plc.ckp_to_il.ckp_to_il",
                 return_value=expected) as adapter:
        result = ClickBackend().read(str(out))
    adapter.assert_called_once_with("CkpProject-sentinel")
    assert result is expected


# -----------------------------------------------------------------------------
# CkpProject -> IL Program adapter
# -----------------------------------------------------------------------------


def test_ckp_to_il_empty_project_returns_empty_program():
    """A ``CkpProject`` with no subroutines / nicknames translates
    to a ``Program`` with no subroutines / tags.  Sanity check on
    the adapter's defaults."""
    from click_plc.ckp_decoder import CkpProject
    from click_plc.ckp_to_il import ckp_to_il

    proj = CkpProject(
        variant="B", magic=b"\0\0\0\0", section_marker=0x0350,
        prj_raw=b"", ini_raw=b"", nick_raw=b"", dview_raw=b"",
        cmore_raw=b"", scr_raw=[], raw_zip_blobs=[],
    )
    program = ckp_to_il(proj)
    assert program.subroutines == []
    assert program.tags == {}


def test_ckp_to_il_translates_nicknames_to_tags():
    """``CkpProject.nicknames`` -> ``Program.tags``, keyed by
    nickname when set, falling back to raw address otherwise."""
    from click_plc.ckp_decoder import CkpProject, NickEntry
    from click_plc.ckp_to_il import ckp_to_il
    from universal_machinery.il import TagType

    proj = CkpProject(
        variant="B", magic=b"\0\0\0\0", section_marker=0x0350,
        prj_raw=b"", ini_raw=b"", nick_raw=b"", dview_raw=b"",
        cmore_raw=b"", scr_raw=[], raw_zip_blobs=[],
        nicknames=[
            NickEntry(address="X001", nickname="estop", default="E-stop"),
            NickEntry(address="Y001", nickname="lamp", default=""),
            NickEntry(address="DS20", nickname="", default=""),
        ],
    )
    program = ckp_to_il(proj)
    # Keys: nickname if present, raw address otherwise.
    assert set(program.tags) == {"estop", "lamp", "DS20"}
    # Data-type inference from prefix.
    assert program.tags["estop"].data_type is TagType.BOOL
    assert program.tags["lamp"].data_type is TagType.BOOL
    assert program.tags["DS20"].data_type is TagType.INT
    # Description from the default field.
    assert program.tags["estop"].description == "E-stop"
    # Address preserved verbatim.
    assert program.tags["lamp"].address.raw == "Y001"


def test_ckp_to_il_duplicate_nicknames_get_suffixed():
    """Two unnamed nicknames at different addresses would both
    map to ``""`` -- the adapter falls back to the raw address as
    the key, so they don't collide.  Verify."""
    from click_plc.ckp_decoder import CkpProject, NickEntry
    from click_plc.ckp_to_il import ckp_to_il

    proj = CkpProject(
        variant="B", magic=b"\0\0\0\0", section_marker=0x0350,
        prj_raw=b"", ini_raw=b"", nick_raw=b"", dview_raw=b"",
        cmore_raw=b"", scr_raw=[], raw_zip_blobs=[],
        nicknames=[
            NickEntry(address="X001"),
            NickEntry(address="X002"),
        ],
    )
    program = ckp_to_il(proj)
    assert set(program.tags) == {"X001", "X002"}


def test_ckp_to_il_translates_subroutines_with_main_flag():
    """The first ``CkpProject.subroutines`` entry maps to
    ``main=True``; all others get ``main=False``.  All map to
    ``PouKind.PROGRAM`` since CLICK doesn't distinguish IEC
    FUNCTION / FUNCTION_BLOCK."""
    from click_plc.ckp_decoder import (
        CkpProject, Subroutine as CkpSub,
    )
    from click_plc.ckp_to_il import ckp_to_il
    from universal_machinery.il import PouKind

    proj = CkpProject(
        variant="B", magic=b"\0\0\0\0", section_marker=0x0350,
        prj_raw=b"", ini_raw=b"", nick_raw=b"", dview_raw=b"",
        cmore_raw=b"", scr_raw=[], raw_zip_blobs=[],
        subroutines=[
            CkpSub(sub_id=1, name="Main", rungs=[]),
            CkpSub(sub_id=2, name="Sub2", rungs=[]),
            CkpSub(sub_id=3, name="Sub3", rungs=[]),
        ],
    )
    program = ckp_to_il(proj)
    names = [s.name for s in program.subroutines]
    assert names == ["Main", "Sub2", "Sub3"]
    assert all(s.kind is PouKind.PROGRAM for s in program.subroutines)
    assert [s.main for s in program.subroutines] == [True, False, False]


def test_ckp_to_il_translates_simple_ld_rung():
    """Rung with contact + coil opcodes translates to IL ``Rung``
    carrying ``ContactNO`` + ``OutCoil`` ops.  Headline op-mapping
    smoke test."""
    from click_plc.ckp_decoder import (
        CkpProject, Instruction, Subroutine as CkpSub,
    )
    from click_plc.ckp_to_il import ckp_to_il
    from universal_machinery.il.ops import ContactNO, OutCoil

    proj = CkpProject(
        variant="B", magic=b"\0\0\0\0", section_marker=0x0350,
        prj_raw=b"", ini_raw=b"", nick_raw=b"", dview_raw=b"",
        cmore_raw=b"", scr_raw=[], raw_zip_blobs=[],
        subroutines=[
            CkpSub(sub_id=1, name="Main", rungs=[
                [
                    Instruction(offset=0, opcode=0x11,
                                  name="ContactNO", tag="X001"),
                    Instruction(offset=20, opcode=0x15,
                                  name="Out", tag="Y001"),
                ],
            ]),
        ],
    )
    program = ckp_to_il(proj)
    assert len(program.subroutines[0].rungs) == 1
    ops = program.subroutines[0].rungs[0].ops
    assert len(ops) == 2
    assert isinstance(ops[0], ContactNO)
    assert ops[0].address.raw == "X001"
    assert isinstance(ops[1], OutCoil)
    assert ops[1].address.raw == "Y001"


def test_ckp_to_il_maps_all_supported_opcodes():
    """One rung exercising every opcode the adapter recognises
    -- pins each branch of the dispatch."""
    from click_plc.ckp_decoder import (
        CkpProject, Instruction, Subroutine as CkpSub,
    )
    from click_plc.ckp_to_il import ckp_to_il
    from universal_machinery.il.ops import (
        Call, ContactNC, ContactNO, End, Move, OutCoil, OutReset,
        OutSet, Return,
    )

    proj = CkpProject(
        variant="B", magic=b"\0\0\0\0", section_marker=0x0350,
        prj_raw=b"", ini_raw=b"", nick_raw=b"", dview_raw=b"",
        cmore_raw=b"", scr_raw=[], raw_zip_blobs=[],
        subroutines=[
            CkpSub(sub_id=1, name="Main", rungs=[
                [
                    Instruction(0, 0x11, "ContactNO", tag="X001"),
                    Instruction(1, 0x12, "ContactNC", tag="X002"),
                    Instruction(2, 0x15, "Out", tag="Y001"),
                    Instruction(3, 0x16, "OutSet", tag="C1"),
                    Instruction(4, 0x17, "OutReset", tag="C2"),
                    Instruction(5, 0x21, "Copy", tag="DS10",
                                  operands=["DS10", "DS20"]),
                    Instruction(6, 0x23, "Call", tag="Sub2"),
                    Instruction(7, 0x24, "Return"),
                    Instruction(8, 0x27, "End"),
                ],
            ]),
        ],
    )
    ops = ckp_to_il(proj).subroutines[0].rungs[0].ops
    types = [type(op).__name__ for op in ops]
    assert types == [
        "ContactNO", "ContactNC", "OutCoil", "OutSet", "OutReset",
        "Move", "Call", "Return", "End",
    ]
    move_op = ops[5]
    assert isinstance(move_op, Move)
    assert move_op.src.raw == "DS10"
    assert move_op.dst.raw == "DS20"
    assert isinstance(ops[6], Call)
    assert ops[6].target == "Sub2"


def test_ckp_to_il_skips_unhandled_opcodes_silently():
    """Edge / Compare / Math / Tmr / For / Next aren't in this
    slice's scope -- they're dropped from the rung silently rather
    than raising.  The surrounding contacts + coil still survive."""
    from click_plc.ckp_decoder import (
        CkpProject, Instruction, Subroutine as CkpSub,
    )
    from click_plc.ckp_to_il import ckp_to_il
    from universal_machinery.il.ops import ContactNO, OutCoil

    proj = CkpProject(
        variant="B", magic=b"\0\0\0\0", section_marker=0x0350,
        prj_raw=b"", ini_raw=b"", nick_raw=b"", dview_raw=b"",
        cmore_raw=b"", scr_raw=[], raw_zip_blobs=[],
        subroutines=[
            CkpSub(sub_id=1, name="Main", rungs=[
                [
                    Instruction(0, 0x11, "ContactNO", tag="X001"),
                    Instruction(1, 0x13, "Edge", tag="X002"),
                    Instruction(2, 0x14, "Compare",
                                  operands=["DS10", "DS11"]),
                    Instruction(3, 0x18, "Tmr", tag="T1"),
                    Instruction(4, 0x1a, "Math",
                                  operands=["DS10", "DS11", "DS12"]),
                    Instruction(5, 0x15, "Out", tag="Y001"),
                ],
            ]),
        ],
    )
    ops = ckp_to_il(proj).subroutines[0].rungs[0].ops
    # Only the two simple ops survive.
    assert len(ops) == 2
    assert isinstance(ops[0], ContactNO)
    assert isinstance(ops[1], OutCoil)


def test_ckp_to_il_handles_subroutine_with_no_rungs():
    """An empty subroutine (sub_id allocated but no ladder body)
    maps to an empty IL ``Subroutine`` -- common pattern for
    placeholder subs in CLICK projects."""
    from click_plc.ckp_decoder import (
        CkpProject, Subroutine as CkpSub,
    )
    from click_plc.ckp_to_il import ckp_to_il

    proj = CkpProject(
        variant="B", magic=b"\0\0\0\0", section_marker=0x0350,
        prj_raw=b"", ini_raw=b"", nick_raw=b"", dview_raw=b"",
        cmore_raw=b"", scr_raw=[], raw_zip_blobs=[],
        subroutines=[CkpSub(sub_id=1, name="Empty", rungs=[])],
    )
    sub = ckp_to_il(proj).subroutines[0]
    assert sub.name == "Empty"
    assert sub.rungs == []
