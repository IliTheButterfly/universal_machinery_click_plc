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


def test_click_backend_read_raises_not_implemented_with_pointer(tmp_path):
    """Scaffold contract: ``read()`` raises ``NotImplementedError``
    pointing at the CkpProject→IL bridge roadmap item.  Users who
    want byte-level CKP introspection should use ``decode_ckp`` as
    the error message suggests."""
    from click_plc import ClickBackend
    out = tmp_path / "prog.ckp"
    out.write_bytes(b"")
    with pytest.raises(NotImplementedError, match="CkpProject"):
        ClickBackend().read(str(out))
