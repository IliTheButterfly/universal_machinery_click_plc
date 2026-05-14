"""Decode the PLC hardware configuration of a CLICK ``.ckp`` project.

The project's ``Project.ini`` ``[SystemConfig]`` section lists the slot
contents as numeric module IDs (``Item0`` = power supply, ``Item1`` =
CPU, ``Item2..Item11`` = expansion slots 1..10).  This module maps those
IDs back to AutomationDirect part numbers using a catalog reverse-
engineered from CLICK Programming Software ``SystemConfig.dll`` (the
``tagMODULEIDLIST`` table).  CLICK addresses each expansion slot
``N`` (1-indexed) as ``XN01..XN16`` / ``YN01..YN16``, so slot 3 holds
``X301..X316`` and so on.

Use::

    from click_plc import decode_ckp, describe_hardware
    proj = decode_ckp(open('Project.ckp', 'rb').read())
    hw = describe_hardware(proj)
    print(hw.cpu)         # 'C0-12DD2E-2-D'
    print(hw.slots[3])    # SlotInfo(module='C0-08ND3', addr_prefix='X3', ...)

For an unknown ID, the catalog entry is ``None`` and the slot's
``module`` is the raw integer string ``'?<n>'``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Module catalog: numeric ID → AutomationDirect part number.  Built by
# parsing the ``tagMODULEIDLIST`` table inside CLICK's SystemConfig.dll
# (records of [u16 flags][u16 mid][16-byte name pad]).  Covers the
# CLICK basic series (C0-) and CLICK Plus (C2-) plus power supplies.
CATALOG: dict[int, str] = {
    # ----- discrete input modules -----
      8: 'C0-08ND3',        # 8-pt 24VDC sinking/sourcing input
      9: 'C0-08ND3-1',
     10: 'C0-08NA',         # 8-pt 100-240VAC input
     11: 'C0-08NE3',
     12: 'C0-08SIM',        # simulator
     16: 'C0-16ND3',        # 16-pt 24VDC sinking/sourcing input
     19: 'C0-16NE3',
    # ----- discrete output modules -----
     37: 'C0-04TRS',
     38: 'C0-04TRS-10',
     40: 'C0-08TD1',        # 8-pt 24VDC sinking output
     41: 'C0-08TD1-1',
     44: 'C0-08TA',         # 8-pt AC output
     45: 'C0-08TR',         # 8-pt relay output
     46: 'C0-08TR-3',
     48: 'C0-16TD1',        # 16-pt 24VDC sinking output
     50: 'C0-16TD2',        # 16-pt 24VDC sourcing output
     66: 'C0-08CDR',
     72: 'C0-16CDD1',
     73: 'C0-16CDD2',
    # ----- analog input modules -----
    160: 'C0-04AD',
    162: 'C0-04AD-2',
    167: 'C0-04THM',        # 4-ch thermocouple
    168: 'C0-04RTD',        # 4-ch RTD
    169: 'C0-08AD-1',
    # ----- analog output / combo modules -----
    163: 'C0-04DA-1',
    164: 'C0-04DA-2',
    165: 'C0-4AD2DA-1',     # combo: 4 AI + 2 AO
    166: 'C0-4AD2DA-2',
    # ----- C0 series CPU modules (basic + ethernet variants) -----
    192: 'C0-12DD1E-2-D',
    193: 'C0-12DD2E-2-D',
    194: 'C0-12DRE-2-D',
    195: 'C0-12ARE-2-D',
    209: 'C0-10DD2E-D',
    210: 'C0-10DRE-D',
    211: 'C0-10ARE-D',
    212: 'C0-11DD1E-D',
    213: 'C0-11DD2E-D',
    214: 'C0-11DRE-D',
    215: 'C0-11ARE-D',
    216: 'C0-12DD1E-D',
    217: 'C0-12DD2E-D',
    218: 'C0-12DRE-D',
    219: 'C0-12ARE-D',
    220: 'C0-12DD1E-1-D',
    221: 'C0-12DD2E-1-D',
    222: 'C0-12DRE-1-D',
    223: 'C0-12ARE-1-D',
    225: 'C0-00DD1-D',
    226: 'C0-00DD2-D',
    227: 'C0-00DR-D',
    228: 'C0-00DA-D',
    233: 'C0-01DD1-D',
    234: 'C0-01DD2-D',
    235: 'C0-01DR-D',
    236: 'C0-01DA-D',
    241: 'C0-02DD1-D',
    242: 'C0-02DD2-D',
    243: 'C0-02DR-D',
    244: 'C0-02DA-D',
    # ----- C2 series CLICK Plus CPUs -----
    196: 'C2-01CPU',
    197: 'C2-02CPU',
    198: 'C2-03CPU',
    199: 'C2-01CPU-2',
    200: 'C2-02CPU-2',
    201: 'C2-03CPU-2',
    # ----- power supplies -----
    256: 'C0-00AC',         # 0.5A AC supply
    512: 'C0-01AC',         # 1.3A AC supply
    768: 'External P/S',
   1024: '(none)',          # placeholder when CPU is DC-powered ("-D")
}


@dataclass
class SlotInfo:
    """One physical slot in the CLICK base unit.

    `index` is the slot number using CLICK's user-facing numbering:
    ``0`` = power-supply slot (special), ``1`` = CPU, ``2..11`` =
    expansion slots 1..10.

    `addr_prefix` is the CLICK address prefix this slot's I/O appears
    under in the ladder logic: ``'X3'``/``'Y3'`` for slot 3, etc.
    None for the PS slot and CPU slot, which use ``X001..`` / ``Y001..``.
    """
    index: int
    raw_id: int
    module: Optional[str]                 # part number, or None if unknown
    addr_prefix: Optional[str] = None     # e.g. "X3" / "Y3" for expansion slot 3


@dataclass
class HardwareConfig:
    """Decoded hardware configuration for a CKP project."""
    plc_name: str
    cpu: Optional[str]                    # CPU module part number
    cpu_id: int                           # raw CPU ID from the project
    power_supply: Optional[str]
    slots: list[SlotInfo]                 # all 12 slot positions, in order
    cpu_category: int                     # 0 = basic CLICK, 1 = ?  (raw value)

    def expansion_slots(self) -> list[SlotInfo]:
        """Return only populated expansion slots (skip PS, CPU, and empty)."""
        return [s for s in self.slots
                if s.index >= 2 and s.module not in (None, '(none)') and s.raw_id != 0]

    def summary(self) -> str:
        """Multi-line human-readable hardware summary."""
        lines = [f'PLC name:      {self.plc_name}']
        lines.append(f'CPU:           {self.cpu or f"unknown (ID={self.cpu_id})"}')
        if self.power_supply and self.power_supply != '(none)':
            lines.append(f'Power supply:  {self.power_supply}')
        lines.append('Expansion slots:')
        exp = self.expansion_slots()
        if not exp:
            lines.append('  (none)')
        else:
            for s in exp:
                lines.append(f'  slot {s.index - 1:>2}  '
                             f'addr={s.addr_prefix or "-"}  '
                             f'{s.module or f"unknown ID={s.raw_id}"}')
        return '\n'.join(lines)


def lookup_module(module_id: int) -> Optional[str]:
    """Map a CLICK module ID to its part number, or None if unknown."""
    return CATALOG.get(module_id)


def describe_hardware(project) -> HardwareConfig:
    """Extract the hardware configuration from a parsed CkpProject.

    Reads the ``[SystemConfig]`` and ``[PLCName]`` sections of the
    project's embedded ``Project.ini``.

    The 12 ``Item<n>`` entries represent slots in this order:
      - Item0: power supply (256/512 = AC; 768 = external; 1024 = DC CPU)
      - Item1: CPU module
      - Item2..Item11: expansion slots 1..10
    """
    plc_name = (project.get_ini_value('PLCName', 'PLCName') or '').strip()

    def _int(section: str, key: str, default: int = 0) -> int:
        val = project.get_ini_value(section, key)
        try:
            return int(val) if val is not None else default
        except ValueError:
            return default

    cpu_category = _int('SystemConfig', 'CPUCategory')
    items = [_int('SystemConfig', f'Item{i}') for i in range(12)]

    ps_id, cpu_id = items[0], items[1]
    slots: list[SlotInfo] = []

    # Slot 0: power supply (or "(none)" for DC-powered CPUs)
    slots.append(SlotInfo(index=0, raw_id=ps_id, module=lookup_module(ps_id)))
    # Slot 1: CPU
    slots.append(SlotInfo(index=1, raw_id=cpu_id, module=lookup_module(cpu_id)))
    # Slots 2..11: expansion slots, addressed XN01.. / YN01.. with N=index-1
    for i, raw_id in enumerate(items[2:], start=2):
        if raw_id == 0:
            slots.append(SlotInfo(index=i, raw_id=0, module=None))
        else:
            n = i - 1   # CLICK's 1-indexed expansion slot number
            slots.append(SlotInfo(
                index=i, raw_id=raw_id,
                module=lookup_module(raw_id),
                addr_prefix=f'X{n}/Y{n}',
            ))

    return HardwareConfig(
        plc_name=plc_name,
        cpu=lookup_module(cpu_id),
        cpu_id=cpu_id,
        power_supply=lookup_module(ps_id),
        slots=slots,
        cpu_category=cpu_category,
    )
