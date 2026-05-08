"""Decoder/editor for AutomationDirect CLICK PLC project files (.ckp).

Status: READ is fully reliable.  WRITE recomputes the file's magic
checksum (XOR16 of u16 LE words from offset 2 to end -- reverse-engineered
from CLICK.exe FUN_004fe470) so unedited round-trips are byte-identical
and edited files have a valid magic.  Project.ini security tokens
(wsep/wseu/emsep/emseu) are NOT regenerated; they may need to be valid
for EB Pro to accept the file -- this hasn't been confirmed yet.

Two container variants:

  Variant A (older EB Pro, 'vPt\\0' magic):
      SC-PRJ at 0x074, per-section header word 0x0350.
      Embeds three trailing zip blobs (node-RED + OPC-UA + a
      password-protected SC_.mdb cache that this module ignores).

  Variant B (newer EB Pro, 4-byte version magic e.g. ac 72 5c 00):
      SC-PRJ at 0x05c, per-section header word 0x0328.
      No trailing zips; SC_.mdb is rebuilt by EB Pro from the SC-*
      sections at project-open time, in a 'CLICK (xxxxxxxx)' temp dir.

Both variants share the same 0x06+ section table layout and the same
SC-* body formats.

Decoding (CLI):
    python ckp_decoder.py path/to/Project.ckp [output_dir]

Decoding (library):
    project = decode_ckp(open('Project.ckp', 'rb').read())

Editing (programmatic, but EB Pro won't open the result -- see status):
    project.set_ini_value('SystemConfig', 'AssignNick', '0')
    project.add_tag('MyFlag', 'DISCRETE', 'C200')
    project.remove_tag('OldFlag')
    project.add_dview('my_view.cdv')
    open('out.ckp', 'wb').write(project.encode())

Round-tripping with no edits is byte-identical.

The high-level editors (set_ini_value, add_tag, ...) rebuild the raw
bytes of the affected section.  SC-PRJ, SC-NICK, and SC-SCR are kept
as raw bytes only; mutate `project.<section>_raw` directly to edit
those.  An `insert_no_out_rung_before_terminator()` helper exists for
the specific case of inserting a "ContactNO -> Out" 2-instruction rung,
templated from a known-good EB Pro reference.
"""

from __future__ import annotations

import os
import re
import struct
import sys
from dataclasses import dataclass, field
from typing import ClassVar, Optional


# --- low-level helpers -----------------------------------------------------

def u16(data: bytes, off: int) -> int:
    return struct.unpack_from('<H', data, off)[0]

def u32(data: bytes, off: int) -> int:
    return struct.unpack_from('<I', data, off)[0]


# --- ladder logic decoding -------------------------------------------------

LADDER_OPCODES = {
    0x11: "ContactNO", 0x12: "ContactNC", 0x15: "Out",
    0x23: "Call", 0x24: "Return", 0x27: "End",
}
TAGGED_OPCODES = {0x11, 0x12, 0x15}
RUNG_END = re.compile(rb'\x20\x00(?:\x01[\x00-\x1f]){32}')
_OPCODE_NAME_LENS = {0x07, 0x09, 0x0d, 0x13}


@dataclass
class Instruction:
    offset: int
    opcode: int
    name: str
    tag: Optional[str] = None


@dataclass
class Subroutine:
    sub_id: int
    name: str
    rungs: list[list[Instruction]] = field(default_factory=list)


# --- High-level Rung API used by CkpProject.add_rung() -------------------

@dataclass
class _Field:
    """One variable-byte field in a rung byte template.

    kind:
      'tag'       — 3- or 4-char memory address pstr at offset (length-prefix
                    inclusive); writes 2*n_chars+1 byte at offset
      'pos'       — u16 LE position word at offset
      'rung_idx'  — single byte at offset (1-indexed rung number)
      'callmeta'  — 1 byte = utf-16-le-byte-count of target sub name
      'subname'   — pstr at offset for Call's target subroutine name
    """
    kind: str
    offset: int
    n_chars: int = 0    # for tag/subname: char count (UTF-16LE byte count = 2*n)


@dataclass
class _RungTemplate:
    """A verbatim byte sequence + variable-field descriptors for one rung kind.

    fixed_patches: list of (offset, value) pairs that should be unconditionally
    applied when building from this template -- used for fields whose value
    depends on the insert position (e.g., "distance from end of rungs"
    counter, which is always 2 when inserting before the terminator).
    """
    name: str
    raw: bytes
    fields: dict[str, _Field]
    fixed_patches: list = field(default_factory=list)


@dataclass
class Rung:
    """A high-level rung specification.  Build via class methods, then pass
    to project.add_rung(sub_id, rung)."""
    template_name: str
    tags: dict[str, str] = field(default_factory=dict)        # named tags ('a','b','c','src','dst')
    target_sub: str = ''                                      # for Call
    memory_refs: list[str] = field(default_factory=list)       # tags to add to SC-NICK

    @classmethod
    def no_out(cls, no_tag: str, out_tag: str) -> 'Rung':
        """ContactNO(no_tag) -> COIL(out_tag).  Both tags must be 3 chars."""
        if len(no_tag) != 3 or len(out_tag) != 3:
            raise ValueError("no_out: both tags must be exactly 3 characters")
        return cls('no_out', tags={'no': no_tag, 'out': out_tag},
                   memory_refs=[no_tag, out_tag])

    @classmethod
    def no_nc_out(cls, no_tag: str, nc_tag: str, out_tag: str) -> 'Rung':
        """ContactNO(no_tag) AND NOT ContactNC(nc_tag) -> COIL(out_tag).
        Required tag lengths: 3, 4, 4 (templated from C10/X001/C100)."""
        if len(no_tag) != 3 or len(nc_tag) != 4 or len(out_tag) != 4:
            raise ValueError("no_nc_out: tag lengths must be 3, 4, 4 "
                             f"(got {len(no_tag)}, {len(nc_tag)}, {len(out_tag)})")
        return cls('no_nc_out', tags={'no': no_tag, 'nc': nc_tag, 'out': out_tag},
                   memory_refs=[no_tag, nc_tag, out_tag])

    @classmethod
    def copy(cls, src_tag: str, dst_tag: str) -> 'Rung':
        """Copy(src_tag) -> dst_tag.  Both tags must be 4 chars (templated
        from DS20/DS21)."""
        if len(src_tag) != 4 or len(dst_tag) != 4:
            raise ValueError("copy: both tags must be exactly 4 characters")
        return cls('copy', tags={'src': src_tag, 'dst': dst_tag},
                   memory_refs=[src_tag, dst_tag])

    @classmethod
    def call(cls, target_sub: str) -> 'Rung':
        """Call the given subroutine."""
        if len(target_sub) != 4:
            raise ValueError("call: target_sub must be 4 characters "
                             "(template uses 'Sub1')")
        return cls('call', target_sub=target_sub)

    def referenced_memory_tags(self) -> list[str]:
        return list(self.memory_refs)

    def build(self, project: 'CkpProject', scr_blob: bytes, rung_index: int) -> bytes:
        """Render this rung to bytes by substituting into the template."""
        tmpl = project._rung_templates[self.template_name]
        out = bytearray(tmpl.raw)

        # Compute base position from existing scr_blob
        base_pos = project._next_position(scr_blob, gap=0x10)

        # Apply tag substitutions
        for name, tag in self.tags.items():
            f = tmpl.fields[name]
            if f.kind != 'tag':
                continue
            content = tag.encode('utf-16-le') + b'\x00'
            out[f.offset : f.offset + len(content)] = content

        # Apply Call subname + callmeta
        if self.target_sub:
            f = tmpl.fields['subname']
            content = self.target_sub.encode('utf-16-le') + b'\x00'
            # length prefix is 1 byte BEFORE subname offset
            out[f.offset - 1] = len(content)
            out[f.offset : f.offset + len(content)] = content
            cm = tmpl.fields.get('callmeta')
            if cm is not None:
                out[cm.offset] = (len(self.target_sub) * 2) & 0xff

        # Position fields: preserve the template's relative position deltas
        # (they reflect EB Pro's internal spacing convention which differs by
        # instruction type) and shift the whole sequence by `base_pos`.
        pos_fields = [(name, f) for name, f in tmpl.fields.items() if f.kind == 'pos']
        pos_fields.sort(key=lambda nf: nf[1].offset)
        if pos_fields:
            # The first position in the template
            first_template_pos = struct.unpack_from('<H', tmpl.raw, pos_fields[0][1].offset)[0]
            for _, f in pos_fields:
                tpl_value = struct.unpack_from('<H', tmpl.raw, f.offset)[0]
                delta = tpl_value - first_template_pos
                struct.pack_into('<H', out, f.offset, (base_pos + delta) & 0xffff)

        # rung_idx fields (the template may have multiple, one per
        # instruction's trailer in multi-cell rungs)
        for f in tmpl.fields.values():
            if f.kind == 'rung_idx':
                out[f.offset] = rung_index & 0xff

        # Apply unconditional fixed-value patches
        for off, val in tmpl.fixed_patches:
            out[off] = val

        return bytes(out)


# Verbatim rung templates extracted from a known-good EB Pro reference file.
# Each template is the FULL rung block: 14-byte header + instructions +
# 66-byte rung-end bitmap + 66-byte post-padding.  Field offsets are
# computed from the structure, not memorised: see _build_rung_templates().

_RUNG_NO_OUT_HEX = "0200f600000001000101000000001343006f006e0074006100630074004e004f000011270000000000000101680300006560074300320031000000000000011f010101000000074f0075007400001527000000000000010194030000666007430032003200000000000003000000000002000300000120000100010101020103010401050106010701080109010a010b010c010d010e010f0110011101120113011401150116011701180119011a011b011c011d011e011f200000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
_RUNG_NO_NC_OUT_HEX = "0300f600000001000101000000001343006f006e0074006100630074004e004f0000112700000000000001015d010000656007430031003000000000000001010101010000001343006f006e0074006100630074004e004f0000122700000000000001019701000065600958003000300031000000000000011f010102000000074f00750074000015270000000000000101c50100006660094300310030003000000000000001000000000003000300000120000100010101020103010401050106010701080109010a010b010c010d010e010f0110011101120113011401150116011701180119011a011b011c011d011e011f200000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
_RUNG_COPY_HEX     = "0100f6000000011f0202000000000943006f007000790000212700000000000001019a0200007460094400530032003000007660094400530032003100000000000002000000000002000300000120000100010101020103010401050106010701080109010a010b010c010d010e010f0110011101120113011401150116011701180119011a011b011c011d011e011f200000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
_RUNG_CALL_HEX     = "01008c000000011f01010000000009430061006c006c00002327000000000000010169010000193200000862095300750062003100000000000001000000000002000300000120000100010101020103010401050106010701080109010a010b010c010d010e010f0110011101120113011401150116011701180119011a011b011c011d011e011f200000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"


def _build_rung_templates() -> 'dict[str, _RungTemplate]':
    """Build the dict of rung templates by parsing each verbatim hex blob
    and locating the variable-field offsets within it."""
    def clean(hex_str): return bytes.fromhex(hex_str.replace('\n', '').replace(' ', ''))

    out: 'dict[str, _RungTemplate]' = {}

    # NO + Out (2-cell)
    raw = clean(_RUNG_NO_OUT_HEX)
    out['no_out'] = _RungTemplate(
        name='no_out', raw=raw,
        fields={
            'no':       _Field('tag', 0x33, 3),
            'out':      _Field('tag', 0x5f, 3),
            'pos_no':   _Field('pos', 0x2c),
            'pos_out':  _Field('pos', 0x58),
            'rung_idx': _Field('rung_idx', 0x6a),
        }
    )

    # NO + NC + Out (3-cell).  Only the end-of-rung trailer of the Out
    # instruction holds the rung index; the per-instruction inter-instr
    # trailers carry column-position info (a fixed 0x01 at byte 4).
    raw = clean(_RUNG_NO_NC_OUT_HEX)
    out['no_nc_out'] = _RungTemplate(
        name='no_nc_out', raw=raw,
        fields={
            'no':       _Field('tag', 0x33, 3),
            'pos_no':   _Field('pos', 0x2c),
            'nc':       _Field('tag', 0x6b, 4),
            'pos_nc':   _Field('pos', 0x64),
            'out':      _Field('tag', 0x99, 4),
            'pos_out':  _Field('pos', 0x92),
            'rung_idx': _Field('rung_idx', 0xa6),
        },
        # byte 0xac is "distance from end of rungs" (= total - rung_idx + 1).
        # For "insert before terminator", this is always 2.  Template was
        # extracted from rung 1 of 3 (= 3) so we patch it to 2.
        fixed_patches=[(0xac, 0x02)],
    )

    # Copy (1 instr, 2 operands)
    raw = clean(_RUNG_COPY_HEX)
    out['copy'] = _RungTemplate(
        name='copy', raw=raw,
        fields={
            'src':      _Field('tag', 0x29, 4),
            'dst':      _Field('tag', 0x35, 4),
            'pos':      _Field('pos', 0x22),
            'rung_idx': _Field('rung_idx', 0x42),
        }
    )

    # Call (1 instr, target subroutine name)
    raw = clean(_RUNG_CALL_HEX)
    out['call'] = _RungTemplate(
        name='call', raw=raw,
        fields={
            'subname':  _Field('subname', 0x2d, 4),
            'pos':      _Field('pos', 0x22),
            'callmeta': _Field('callmeta', 0x2a),
            'rung_idx': _Field('rung_idx', 0x3a),
        }
    )

    return out


def _read_pstr_utf16le(data: bytes, off: int) -> str:
    n = data[off]
    return data[off+1:off+n].decode('utf-16-le', errors='replace').rstrip('\x00')


def _scan_instructions(data: bytes, lo: int, hi: int) -> list[Instruction]:
    out: list[Instruction] = []
    p = lo
    while p < hi - 2:
        ln = data[p]
        if ln not in _OPCODE_NAME_LENS or p + ln + 4 >= hi:
            p += 1; continue
        try:
            name = data[p+1:p+ln].decode('utf-16-le').rstrip('\x00')
        except Exception:
            p += 1; continue
        if name not in LADDER_OPCODES.values():
            p += 1; continue
        op_off = p + ln + 1
        op = data[op_off]
        if op not in LADDER_OPCODES:
            p += 1; continue
        tag = None
        if op in TAGGED_OPCODES:
            tag_len_off = op_off + 16
            if tag_len_off < hi:
                tlen = data[tag_len_off]
                if tlen and tag_len_off + tlen < hi:
                    try:
                        tag = (data[tag_len_off+1:tag_len_off+tlen]
                               .decode('utf-16-le').rstrip('\x00'))
                    except Exception:
                        pass
        elif op == 0x23:
            for q in range(op_off + 17, min(op_off + 60, hi)):
                tl = data[q]
                if tl in (9, 0x0d, 0x13, 0x19, 0x21, 0x29):
                    try:
                        t = data[q+1:q+tl].decode('utf-16-le').rstrip('\x00')
                        if t and t.isprintable():
                            tag = t; break
                    except Exception:
                        pass
        out.append(Instruction(p, op, LADDER_OPCODES[op], tag))
        p = op_off + 1
    return out


def parse_subroutine(data: bytes, off: int, size: int) -> Subroutine:
    end = off + size
    sub_id = u16(data, off + 0x40)
    name = _read_pstr_utf16le(data, off + 0x42)
    breaks = [m.end() for m in RUNG_END.finditer(data, off, end)]
    rungs = []
    for a, b in zip([off] + breaks, breaks + [end]):
        instrs = _scan_instructions(data, a, b)
        if instrs:
            rungs.append(instrs)
    return Subroutine(sub_id, name, rungs)


def render_rung(instrs: list[Instruction]) -> str:
    contacts, outputs = [], []
    for ins in instrs:
        if ins.opcode == 0x11:    contacts.append(ins.tag)
        elif ins.opcode == 0x12:  contacts.append(f"NOT {ins.tag}")
        elif ins.opcode == 0x15:  outputs.append(f"COIL({ins.tag})")
        elif ins.opcode == 0x23:  outputs.append(f"CALL {ins.tag}")
        elif ins.opcode == 0x24:  outputs.append("RETURN")
        elif ins.opcode == 0x27:  outputs.append("END")
    lhs = " AND ".join(c for c in contacts if c)
    rhs = " ; ".join(outputs)
    if lhs and rhs:  return f"{lhs}  ->  {rhs}"
    return rhs or lhs or "(empty)"


# --- SC-INI / SC-DVIEW / SC-CMORE / SC-NICK --------------------------------

def parse_ini(data: bytes, off: int, size: int) -> tuple[str, bytes]:
    p = off + 0x40
    fn_len = data[p]
    fn = data[p+1:p+fn_len].decode('utf-16-le').rstrip('\x00')
    p = p + fn_len + 1
    content_len = u32(data, p)
    return fn, data[p+4:p+4+content_len]


def parse_dview(data: bytes, off: int, size: int) -> list[str]:
    end = off + size
    p = off + 0x40
    files: list[str] = []
    while p < end:
        n = data[p]
        if n == 0:
            p += 1; continue
        if n < 5 or n > 0x40 or p + n > end:
            p += 1; continue
        try:
            s = data[p+1:p+n].decode('utf-16-le').rstrip('\x00')
        except Exception:
            p += 1; continue
        if s.isprintable() and any(c in s for c in ('.', '/')):
            files.append(s)
        p += n
    return files


def parse_cmore(data: bytes, off: int, size: int) -> str:
    p = off + 0x40
    csv_len = u32(data, p)
    return data[p+4:p+4+csv_len].decode('utf-8')


@dataclass
class NickEntry:
    address: str
    nickname: str = ''
    default: str = ''


def parse_nick(data: bytes, off: int, size: int) -> list[NickEntry]:
    end = off + size
    parts: list[str] = []
    p = off + 0x40
    while p < end - 1:
        n = data[p]
        if n < 3 or n > 0x40 or n % 2 == 0 or p + 1 + n > end:
            p += 1; continue
        try:
            s = data[p+1:p+n].decode('utf-16-le').rstrip('\x00')
        except Exception:
            p += 1; continue
        if s and all(0x20 <= ord(c) < 0x7f for c in s):
            parts.append(s)
            p += 1 + n
        else:
            p += 1
    entries: list[NickEntry] = []
    j = 0
    while j + 1 < len(parts):
        addr = parts[j] + parts[j+1]
        if j + 2 < len(parts) and parts[j+2].startswith('_'):
            nick = parts[j+2]
            default = parts[j+3] if j + 3 < len(parts) else ''
            j += 4
        else:
            nick = default = ''
            j += 2
        entries.append(NickEntry(addr, nick, default))
    return entries


# --- top-level container --------------------------------------------------

# Each project section is stored as the verbatim bytes that originally
# appeared between section boundaries (including the 0x40-byte section
# header).  encode() concatenates those raw bytes and rewrites the section
# table at the start of the file.
#
# High-level editors (set_ini_value, add_tag, etc.) rebuild the raw bytes
# of the section they touch using minimal-change strategies: they preserve
# the original section header and any inter-entry padding bytes that we
# don't fully understand, replacing only the fields we know how to rewrite.

@dataclass
class CkpProject:
    variant: str                # 'A' or 'B'
    magic: bytes                # original 4-byte magic, preserved on encode
    section_marker: int         # 0x0350 or 0x0328

    # Raw section bytes (full section incl. 0x40 header).  These are the
    # source of truth on encode -- they round-trip verbatim by default.
    prj_raw: bytes
    ini_raw: bytes
    nick_raw: bytes             # b'' on variant A
    dview_raw: bytes
    cmore_raw: bytes
    scr_raw: list[bytes]
    raw_zip_blobs: list[bytes]  # variant A only

    # Decoded views (helper representations; not authoritative).  Mutating
    # these does NOT change what encode() produces -- use the editors below.
    ini_file: str = ''
    ini_body: bytes = b''
    dview_files: list[str] = field(default_factory=list)
    tags_csv: str = ''
    subroutines: list[Subroutine] = field(default_factory=list)
    nicknames: list[NickEntry] = field(default_factory=list)

    # ----- pretty-printing -----

    def render_program(self) -> str:
        out = ["# Decoded ladder logic"]
        for sub in self.subroutines:
            out.append(f"\nSubroutine #{sub.sub_id}: {sub.name}")
            for i, rung in enumerate(sub.rungs, 1):
                out.append(f"  Rung {i}: {render_rung(rung)}")
        return "\n".join(out) + "\n"

    def write(self, out_dir: str) -> None:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, self.ini_file or 'Project.ini'), 'wb') as f:
            f.write(self.ini_body)
        with open(os.path.join(out_dir, 'DataView.list'), 'w') as f:
            f.write("\n".join(self.dview_files) + "\n")
        with open(os.path.join(out_dir, 'tags.csv'), 'w') as f:
            f.write(self.tags_csv)
        with open(os.path.join(out_dir, 'program.txt'), 'w') as f:
            f.write(self.render_program())
        with open(os.path.join(out_dir, 'nicknames.csv'), 'w') as f:
            f.write("address,nickname,default\n")
            for e in self.nicknames:
                f.write(f"{e.address},{e.nickname},{e.default}\n")
        for i, blob in enumerate(self.raw_zip_blobs, 1):
            with open(os.path.join(out_dir, f"zip{i}.zip"), 'wb') as f:
                f.write(blob)

    # ----- INI editors (rebuild ini_raw in place) -----

    def get_ini_value(self, section: str, key: str) -> Optional[str]:
        in_sec = False
        for line in self.ini_body.decode('utf-8', errors='replace').splitlines():
            stripped = line.strip()
            if stripped == f"[{section}]":
                in_sec = True; continue
            if in_sec:
                if stripped.startswith('[') and stripped.endswith(']'):
                    return None
                if '=' in line and line.split('=', 1)[0].strip() == key:
                    return line.split('=', 1)[1].strip()
        return None

    def set_ini_value(self, section: str, key: str, value: str) -> None:
        text = self.ini_body.decode('utf-8', errors='replace')
        eol = '\r\n' if '\r\n' in text else '\n'
        lines = text.split(eol)
        sec_hdr = f"[{section}]"
        in_sec = False
        i_section = -1
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == sec_hdr:
                in_sec = True
                i_section = i
                continue
            if in_sec and stripped.startswith('[') and stripped.endswith(']'):
                lines.insert(i, f"{key}={value}")
                self.ini_body = eol.join(lines).encode('utf-8')
                self._rebuild_ini(); return
            if in_sec and '=' in line and line.split('=', 1)[0].strip() == key:
                lines[i] = f"{key}={value}"
                self.ini_body = eol.join(lines).encode('utf-8')
                self._rebuild_ini(); return
        if i_section < 0:
            if lines and lines[-1] != '':
                lines.append('')
            lines.append(sec_hdr)
            lines.append(f"{key}={value}")
        else:
            lines.append(f"{key}={value}")
        self.ini_body = eol.join(lines).encode('utf-8')
        self._rebuild_ini()

    def _rebuild_ini(self) -> None:
        """Rewrite the body of ini_raw with the current ini_file + ini_body.

        Keeps the original 0x40-byte section header intact.
        """
        header = bytearray(self.ini_raw[:0x40])
        body = bytearray()
        fn_utf16 = self.ini_file.encode('utf-16-le')
        body.append(len(fn_utf16) + 1)            # prefix-includes-trailing-zero
        body.extend(fn_utf16)
        body.append(0)                            # trailing zero of the pstr
        body.extend(struct.pack('<I', len(self.ini_body)))
        body.extend(self.ini_body)
        self.ini_raw = bytes(header) + bytes(body)

    # ----- CMORE (tags) editors -----

    def list_tags(self) -> list[tuple[str, str, str]]:
        return [tuple(line.split(',', 2))
                for line in self.tags_csv.splitlines()
                if line and ',' in line]

    def add_tag(self, name: str, type_: str, address: str) -> None:
        eol = '\r\n' if '\r\n' in self.tags_csv else '\n'
        if not self.tags_csv.endswith(eol) and self.tags_csv:
            self.tags_csv += eol
        self.tags_csv += f"{name},{type_},{address}{eol}"
        self._rebuild_cmore()

    def remove_tag(self, name: str) -> bool:
        eol = '\r\n' if '\r\n' in self.tags_csv else '\n'
        kept = [ln for ln in self.tags_csv.split(eol)
                if not ln.startswith(name + ',')]
        new = eol.join(kept)
        changed = new != self.tags_csv
        if changed:
            self.tags_csv = new
            self._rebuild_cmore()
        return changed

    def _rebuild_cmore(self) -> None:
        header = bytearray(self.cmore_raw[:0x40])
        raw = self.tags_csv.encode('utf-8')
        body = struct.pack('<I', len(raw)) + raw
        self.cmore_raw = bytes(header) + body

    # ----- DVIEW editors -----

    def add_dview(self, filename: str) -> None:
        if filename not in self.dview_files:
            self.dview_files.append(filename)
            self._rebuild_dview()

    def remove_dview(self, filename: str) -> bool:
        try:
            self.dview_files.remove(filename)
            self._rebuild_dview()
            return True
        except ValueError:
            return False

    def _rebuild_dview(self) -> None:
        """Rebuild dview_raw.  We don't fully understand the trailer bytes
        between entries, so we only call this when the user actually edits
        the file list -- otherwise the raw bytes round-trip verbatim.
        """
        header = bytearray(self.dview_raw[:0x40])
        body = bytearray(b'\x01\x00')          # observed 2-byte body header
        for fn in self.dview_files:
            fn_utf16 = fn.encode('utf-16-le')
            body.append(len(fn_utf16) + 1)
            body.extend(fn_utf16)
            body.append(0)
            body.extend(b'\x00\x00\x00\x00')   # 4-byte zero trailer per entry
        self.dview_raw = bytes(header) + bytes(body)

    # ----- ladder-logic editors -----
    #
    # SC-SCR per-rung byte layout (reverse-engineered from EB Pro output;
    # see demo/click_tests/TestProject.ckp vs TestProject_with_C21_C22.ckp):
    #
    #   14-byte rung header:
    #     byte 0    : column count (1 for single-cell rungs, 2 for NO+Out, 3 for NO+NC+Out)
    #     bytes 1-6 : 00 f6 00 00 00 01
    #     byte 7    : 0x1f if single-column rung, 0x00 if multi-column
    #     bytes 8-9 : 01 01
    #     bytes 10-13: 00 00 00 00
    #
    #   Per-instruction (NO/NC/Out):
    #     pstr name         (20 bytes for ContactNO/NC, 8 bytes for Out)
    #     opcode + 0x27     (2 bytes)
    #     6 zero meta bytes
    #     01 01             (flags)
    #     position u16 LE   (per-instruction unique id; allocated sequentially)
    #     00 00
    #     data type u16 LE  (0x6065 NO/NC, 0x6066 Out)
    #     pstr tag          (e.g. "C21")
    #     12-byte trailer if not last instruction
    #     16-byte trailer if last instruction in rung (the trailer values
    #       depend on the rung's column structure; we use known-good
    #       reference templates below).
    #
    #   66-byte rung-end bitmap (\x20\x00 + 32 * \x01 NN, NN=0..0x1f)
    #   66-byte post-padding (\x20 + 65 zero bytes)
    #
    # Section-header byte 0x8f counts the number of rungs in the
    # subroutine; insertion must increment it.
    #
    # We do NOT fully understand the trailer-byte layout for arbitrary
    # rung shapes, so we currently support only the (ContactNO + Out)
    # 2-instruction rung pattern, using verbatim bytes captured from a
    # known-good reference file as a template.

    # Verbatim 250-byte rung body (header + NO+Out instructions + bitmap +
    # post-padding) extracted from TestProject_with_C21_C22.ckp's Sub1 at
    # the position where the C21 -> COIL(C22) rung was inserted by EB Pro.
    # Variable bytes: tag1 at [0x33:0x3a], position1 at [0x2c:0x2e],
    # tag2 at [0x5f:0x66], position2 at [0x58:0x5a].
    _NO_OUT_RUNG_TEMPLATE = bytes.fromhex(  # noqa: E501
        "0200f600000001000101000000001343006f006e0074006100630074004e004f000011270000000000000101680300006560074300320031000000000000011f010101000000074f0075007400001527000000000000010194030000666007430032003200000000000003000000000002000300000120000100010101020103010401050106010701080109010a010b010c010d010e010f0110011101120113011401150116011701180119011a011b011c011d011e011f200000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    )
    _NO_OUT_TAG1_OFFSET = 0x33      # 7 bytes (3-char tag's UTF-16LE + 1 trailing zero)
    _NO_OUT_TAG2_OFFSET = 0x5f      # 7 bytes
    _NO_OUT_POS1_OFFSET = 0x2c      # u16 LE
    _NO_OUT_POS2_OFFSET = 0x58      # u16 LE
    # Within the Out instruction's 16-byte end-of-rung trailer, byte 4 is
    # the 1-indexed rung number for this rung.  In our 250-byte template
    # the Out instruction is at template offset 0x46, its trailer at 0x66,
    # so byte 4 of trailer = 0x66 + 4 = 0x6a.
    _NO_OUT_RUNG_INDEX_OFFSET = 0x6a

    @staticmethod
    def _pstr(s: str) -> bytes:
        """Build a length-prefixed UTF-16LE string with the +1 trailing-zero
        convention used throughout SC-* sections."""
        body = s.encode('utf-16-le') + b'\x00'
        return bytes([len(body)]) + body

    def _next_position(self, scr_blob: bytes, gap: int = 0x10) -> int:
        """Return the next free per-instruction position id for `scr_blob`,
        chosen as max(existing positions) + gap.

        Each instruction has a u16 LE "position" id at a fixed offset past
        its UTF-16LE name.  We scan all known opcode names (UTF-16LE +
        trailing 00 null + opcode byte + 0x27 + 6 zero bytes + 01 01) to
        find the position word.
        """
        max_pos = 0
        # name UTF-16LE bytes for each opcode; offset to position word past
        # the end of these bytes is fixed: trailing-null(1) + opcode(1)
        # + 0x27(1) + 6 zero meta + flags 01 01(2) = 11 bytes
        name_specs = [
            ('ContactNO'.encode('utf-16-le'), 11),
            ('Out'.encode('utf-16-le'),       11),
            ('Copy'.encode('utf-16-le'),      11),
            ('Return'.encode('utf-16-le'),    11),
            ('End'.encode('utf-16-le'),       11),
            ('Call'.encode('utf-16-le'),      11),
        ]
        for utf, gap_to_pos in name_specs:
            i = 0
            while True:
                j = scr_blob.find(utf, i)
                if j < 0: break
                pos_off = j + len(utf) + gap_to_pos
                if pos_off + 2 <= len(scr_blob):
                    pos = u16(scr_blob, pos_off)
                    if pos > max_pos:
                        max_pos = pos
                i = j + 1
        return max_pos + gap

    def _build_no_out_rung(self, tag_no: str, tag_out: str,
                           position_no: int, position_out: int,
                           rung_index: int) -> bytes:
        """Build a 2-instruction rung (one ContactNO + one Out coil) by
        cloning the reference template and substituting the tags, positions,
        and 1-indexed rung_index.  Tags must be 3 ASCII characters.
        """
        if len(tag_no) != 3 or len(tag_out) != 3:
            raise ValueError("tags must be exactly 3 characters (template uses 3-char tags)")
        if not (tag_no.isascii() and tag_out.isascii()):
            raise ValueError("tags must be ASCII")

        out = bytearray(self._NO_OUT_RUNG_TEMPLATE)
        # tag content+trailing (UTF-16LE + 1 trailing zero).  6+1 = 7 bytes.
        out[self._NO_OUT_TAG1_OFFSET : self._NO_OUT_TAG1_OFFSET + 6] = (
            tag_no.encode('utf-16-le'))
        out[self._NO_OUT_TAG1_OFFSET + 6] = 0
        out[self._NO_OUT_TAG2_OFFSET : self._NO_OUT_TAG2_OFFSET + 6] = (
            tag_out.encode('utf-16-le'))
        out[self._NO_OUT_TAG2_OFFSET + 6] = 0
        # positions
        struct.pack_into('<H', out, self._NO_OUT_POS1_OFFSET, position_no)
        struct.pack_into('<H', out, self._NO_OUT_POS2_OFFSET, position_out)
        # rung_index in Out's end-of-rung trailer (byte 4 of the 16-byte trailer)
        out[self._NO_OUT_RUNG_INDEX_OFFSET] = rung_index & 0xff
        return bytes(out)

    def find_subroutine(self, sub_id: int) -> int:
        """Return the index of the subroutine in scr_raw matching `sub_id`."""
        for i, blob in enumerate(self.scr_raw):
            if u16(blob, 0x40) == sub_id:
                return i
        raise KeyError(f"no subroutine with id {sub_id}")

    def _add_nick_entry(self, type_str: str, num_str: str) -> None:
        """Append a 2-string SC-NICK entry (memory reference, no nickname).

        Each entry is 19 bytes laid out as observed in EB Pro's output:
            [serial:1] 00 00 02 [pstr type] [pstr num] 00 00 00 00 00 01
        The serial byte increments per entry; the section's count byte
        (offset 0x40 of the NICK section) is also incremented.
        """
        if not self.nick_raw:
            return            # variant A has no NICK section
        type_p = self._pstr(type_str)     # e.g. b'\x03C\x00\x00'
        num_p  = self._pstr(num_str)
        # Compute next serial: scan existing entries for the highest serial.
        # The serial appears as the first byte of every entry (after the
        # 0x40-byte section header and the 4-byte entry-count word).
        existing_count = self.nick_raw[0x40]
        new_serial = existing_count + 0x0d   # observed offset between entry count and entry serials
        entry = (bytes([new_serial]) + b'\x00\x00\x02'
                 + type_p + num_p
                 + b'\x00\x00\x00\x00\x01')
        # Append entry, increment count byte
        new_nick = bytearray(self.nick_raw) + entry
        new_nick[0x40] = (new_nick[0x40] + 1) & 0xff
        self.nick_raw = bytes(new_nick)

    # --------------------------------------------------------------
    #  Generic "insert a rung from a template" mechanism.
    # --------------------------------------------------------------
    #
    # Each rung kind has a verbatim 250-or-so byte template captured from
    # real EB Pro output, plus a list of (offset, kind) "variable fields"
    # that the encoder substitutes per call.  All templates share the same
    # tail layout: ... + 66-byte rung-end bitmap + 66-byte post-padding.
    #
    # Field kinds:
    #   ('tag', n_chars)        substitute a 3-char (or n_chars-char) memory tag pstr
    #                           bytes layout: 1-byte prefix (=2*n+1) + UTF-16LE + 0x00
    #   ('subname', max_chars)  substitute a Call's target subroutine name
    #   ('pos', )               substitute a u16 LE position word
    #   ('rung_index',)         substitute the 1-byte rung index in an end-of-rung trailer
    #   ('callmeta',)           substitute the '08 62' call-target byte-length meta

    # Templates get extracted at module-load time from the known reference
    # file when this class is first used.  See _ensure_templates_loaded().
    _rung_templates: ClassVar['dict[str, _RungTemplate] | None'] = None

    @classmethod
    def _ensure_templates_loaded(cls) -> None:
        if cls._rung_templates is not None:
            return
        cls._rung_templates = _build_rung_templates()

    def add_rung(self, sub_id: int, rung: 'Rung') -> None:
        """Insert a rung built from one of the registered templates,
        immediately before the named subroutine's terminator (Return/End).
        """
        self._ensure_templates_loaded()
        idx = self.find_subroutine(sub_id)
        blob = self.scr_raw[idx]
        bitmaps = list(RUNG_END.finditer(blob))
        if len(bitmaps) < 2:
            raise ValueError("subroutine has no recognizable terminator rung")
        prev_bitmap_end = bitmaps[-2].end()
        scan = prev_bitmap_end
        if scan < len(blob) and blob[scan] == 0x20:
            scan += 1
        while scan < len(blob) and blob[scan] == 0:
            scan += 1
        insertion_offset = scan

        # Build the rung body using its template
        new_rung_bytes = rung.build(
            project=self,
            scr_blob=blob,
            rung_index=blob[0x8f] - 1,
        )

        new_blob = bytearray(blob[:insertion_offset])
        new_blob += new_rung_bytes
        new_blob += blob[insertion_offset:]
        new_blob[0x8f] = (new_blob[0x8f] + 1) & 0xff
        # Update terminator (End/Return) position and rung-counter byte
        rung_size = len(new_rung_bytes)
        for term in ('End', 'Return'):
            utf = term.encode('utf-16-le')
            jj = new_blob.find(utf)
            if jj < 0: continue
            pstr_start = jj - 1
            if pstr_start < 0: continue
            prefix = new_blob[pstr_start]
            pos_off = pstr_start + prefix + 11
            rung_ctr_off = pos_off + 8
            if pos_off + 2 > len(new_blob): continue
            old_pos = struct.unpack_from('<H', new_blob, pos_off)[0]
            struct.pack_into('<H', new_blob, pos_off, (old_pos + rung_size) & 0xffff)
            if rung_ctr_off < len(new_blob):
                new_blob[rung_ctr_off] = (new_blob[rung_ctr_off] + 1) & 0xff
            break
        self.scr_raw[idx] = bytes(new_blob)

        # Auto-add SC-NICK entries for any newly-referenced memory tags
        for tag in rung.referenced_memory_tags():
            type_str = ''.join(c for c in tag if not c.isdigit())
            num_str  = ''.join(c for c in tag if c.isdigit())
            if type_str and num_str:
                self._add_nick_entry(type_str, num_str)

    def add_subroutine_clone(self, source_sub_id: int, new_name: str) -> int:
        """Add a new subroutine by cloning an existing one's SC-SCR section
        verbatim and replacing the sub_id + name.  The new name MUST have
        the same byte length (in UTF-16LE) as the source's name -- the
        section's filler/header bytes are not regenerated.

        Returns the newly-allocated 1-based sub_id.

        Practical use: pick a source whose name length matches what you
        want -- for the demo project's "Sub1" (4 chars) you can clone to
        "Sub2", "Sub3", "MyFn" etc.
        """
        src_idx = self.find_subroutine(source_sub_id)
        src_blob = self.scr_raw[src_idx]
        # name pstr is at section offset 0x42 (1 prefix + content+trail)
        src_prefix = src_blob[0x42]
        src_content_len_bytes = src_prefix - 1   # exclude trailing zero
        # Each char is 2 bytes UTF-16LE
        src_chars = src_content_len_bytes // 2
        if len(new_name) != src_chars:
            raise ValueError(
                f"new_name must be {src_chars} characters to match source "
                f"subroutine '{self.subroutines[src_idx].name}' (got "
                f"{len(new_name)} chars)"
            )

        new_id = max(u16(b, 0x40) for b in self.scr_raw) + 1
        new_blob = bytearray(src_blob)
        struct.pack_into('<H', new_blob, 0x40, new_id)
        # Replace name UTF-16LE bytes (same length, no need to touch prefix or trailing)
        new_blob[0x43:0x43 + src_content_len_bytes] = new_name.encode('utf-16-le')
        self.scr_raw.append(bytes(new_blob))
        return new_id

    def insert_call_rung_before_terminator(self, caller_sub_id: int,
                                           target_sub_name: str) -> None:
        """Insert a CALL <target_sub_name> rung into the named caller
        subroutine, immediately before its terminator (End for Main, Return
        otherwise).  Target name must match an existing subroutine; only
        names of the same byte length as the existing first CALL's target
        are accepted (the Call rung is templated from that existing rung).
        """
        idx = self.find_subroutine(caller_sub_id)
        blob = self.scr_raw[idx]

        # Locate an existing Call instruction in this subroutine to use as
        # template.  Walking the bytes for the UTF-16LE 'Call' name is
        # sufficient since the function-name pstr precedes the opcode byte.
        call_utf = 'Call'.encode('utf-16-le')
        j = blob.find(call_utf)
        if j < 0:
            raise ValueError(
                "no existing Call instruction in this subroutine to template "
                "from -- support for cold-build of Call rung is not implemented"
            )
        # Walk back to the rung header start.  The header is 14 bytes ending
        # right before the pstr prefix byte (j - 1 - 14 = j - 15).
        call_pstr_prefix_off = j - 1
        rung_header_start = call_pstr_prefix_off - 14
        # Walk forward to find this rung's bitmap end (= start of next rung block).
        bitmap = RUNG_END.search(blob, rung_header_start)
        if bitmap is None:
            raise ValueError("could not find Call rung's bitmap")
        # The rung "block" runs from rung_header_start to (post-bitmap padding end).
        # post-bitmap padding is 66 bytes (1 byte 0x20 + 65 zeros).
        rung_body_end = bitmap.end()
        post_pad_end = rung_body_end + 66
        # Extract the full rung block as our template
        template = bytearray(blob[rung_header_start:post_pad_end])

        # Substitute the target name pstr.  Find the existing target name
        # (the second pstr in the rung body, which sits between Call's body
        # and the Call instruction's trailer).
        # Layout in template (offsets relative to template[0] = rung header start):
        #   [14] header
        #   [10] Call pstr (prefix=9 + 9 byte content+trail)
        #   [12] meta (opcode/subop/6 zero/2 flag/2 pos)
        #   [6]  pad+data-type (2 zero + 2 datatype + 2 zero)
        #   [2]  '08 62' meta
        #   [N]  target name pstr  <-- we substitute this
        #   [16] trailer
        meta_end = 14 + 10 + 12 + 6 + 2  # = 44
        target_pstr_off = meta_end
        old_prefix = template[target_pstr_off]
        # New name's UTF-16LE byte count (with +1 trailing zero)
        new_content = target_sub_name.encode('utf-16-le') + b'\x00'
        new_prefix = len(new_content)
        if new_prefix != old_prefix:
            raise ValueError(
                f"target name byte length mismatch: existing call uses a "
                f"{old_prefix}-byte name, requested {new_prefix} bytes"
            )
        template[target_pstr_off] = new_prefix
        template[target_pstr_off + 1 : target_pstr_off + 1 + len(new_content)] = new_content
        # The '08 62' meta encodes the target name's UTF-16LE content byte count
        # (excluding trailing zero) as its low byte.
        template[meta_end - 2] = (new_prefix - 1) & 0xff

        # Update the Call's position word (offset 14+10+10 = 34 from template[0])
        position_off = 14 + 10 + 10
        position = self._next_position(blob, gap=0x10)
        struct.pack_into('<H', template, position_off, position)

        # Compute rung_index for this new rung (it will be inserted before terminator).
        # blob[0x8f] currently = rung_count + 1 (initial bitmap counts as one).
        # New rung index (1-indexed) = current rung count = blob[0x8f] - 1.
        rung_index = blob[0x8f] - 1
        # The Call's end-of-rung trailer's byte 4 is the rung index (same as Out).
        # Trailer offset within template = meta_end + (1 + new_prefix) = 44 + 10 = 54
        trailer_off = meta_end + 1 + new_prefix
        template[trailer_off + 4] = rung_index & 0xff

        # Find insertion point: start of terminator's rung header.  Same logic
        # as for ContactNO/Out insertion.
        bitmaps = list(RUNG_END.finditer(blob))
        if len(bitmaps) < 2:
            raise ValueError("subroutine has no recognizable terminator rung")
        prev_bitmap_end = bitmaps[-2].end()
        scan = prev_bitmap_end
        if scan < len(blob) and blob[scan] == 0x20:
            scan += 1
        while scan < len(blob) and blob[scan] == 0:
            scan += 1
        insertion_offset = scan

        new_blob = bytearray(blob[:insertion_offset])
        new_blob += bytes(template)
        new_blob += blob[insertion_offset:]

        # Increment section's rung-counter byte at 0x8f.
        new_blob[0x8f] = (new_blob[0x8f] + 1) & 0xff

        # Update terminator (End/Return) position and rung-counter byte.
        rung_size = len(template)
        for term in ('End', 'Return'):
            utf = term.encode('utf-16-le')
            jj = new_blob.find(utf)
            if jj < 0: continue
            pstr_start = jj - 1
            if pstr_start < 0: continue
            prefix = new_blob[pstr_start]
            pos_off = pstr_start + prefix + 11
            rung_ctr_off = pos_off + 8
            if pos_off + 2 > len(new_blob): continue
            old_pos = struct.unpack_from('<H', new_blob, pos_off)[0]
            struct.pack_into('<H', new_blob, pos_off, (old_pos + rung_size) & 0xffff)
            if rung_ctr_off < len(new_blob):
                new_blob[rung_ctr_off] = (new_blob[rung_ctr_off] + 1) & 0xff
            break

        self.scr_raw[idx] = bytes(new_blob)

    def insert_no_out_rung_before_terminator(self, sub_id: int,
                                             tag_no: str, tag_out: str) -> None:
        """Insert a rung containing ContactNO(tag_no) -> COIL(tag_out)
        immediately before the terminator (RETURN/END) of the named
        subroutine.  Tags must be 3-character memory addresses like 'C21'.

        Also adds SC-NICK entries for the referenced memory addresses
        (which EB Pro tracks for every referenced address in the program).

        Example::

            project.insert_no_out_rung_before_terminator(sub_id=2,
                tag_no='C21', tag_out='C22')
        """
        idx = self.find_subroutine(sub_id)
        blob = self.scr_raw[idx]
        bitmaps = list(RUNG_END.finditer(blob))
        if len(bitmaps) < 2:
            raise ValueError("subroutine has no recognizable terminator rung")

        # Find start of the terminator's rung header.  After each bitmap
        # there's a 1-byte 0x20 post-marker, then zero padding, then the
        # next rung's header (which begins with a non-zero byte: the
        # column count).  Skip the 0x20 + zeros to find the header.
        prev_bitmap_end = bitmaps[-2].end()
        scan = prev_bitmap_end
        if scan < len(blob) and blob[scan] == 0x20:
            scan += 1
        while scan < len(blob) and blob[scan] == 0:
            scan += 1
        insertion_offset = scan

        position_no = self._next_position(blob, gap=0x10)
        position_out = position_no + 0x2c

        # The new rung's 1-indexed rung number = (current rung count - 1) + 1,
        # since the terminator is the last rung and we're inserting before it.
        # Section header byte 0x8f counts (rungs + 1), so existing rungs = 0x8f - 1.
        # Pre-insertion rung count = blob[0x8f] - 1.  Inserting before terminator
        # makes the new rung the (rung_count - 1 + 1) = rung_count rung (because
        # terminator was at rung_count and new is at rung_count, terminator shifts).
        # Equivalently: new rung index = blob[0x8f] - 1 (since terminator was that).
        rung_index = blob[0x8f] - 1

        new_rung_bytes = self._build_no_out_rung(
            tag_no, tag_out, position_no, position_out, rung_index
        )

        new_blob = bytearray(blob[:insertion_offset])
        new_blob += new_rung_bytes
        new_blob += blob[insertion_offset:]
        # Increment the section's rung-counter byte at offset 0x8f
        new_blob[0x8f] = (new_blob[0x8f] + 1) & 0xff
        # Update the terminator (Return/End) instruction:
        #   position word += inserted_rung_size
        #   rung-counter byte (8 bytes past position) += 1
        rung_size = len(new_rung_bytes)
        for name in ('Return', 'End'):
            utf = name.encode('utf-16-le')
            j = new_blob.find(utf)
            if j < 0:
                continue
            pstr_start = j - 1
            if pstr_start < 0: continue
            prefix = new_blob[pstr_start]
            pos_off = pstr_start + prefix + 11
            rung_ctr_off = pos_off + 8
            if pos_off + 2 > len(new_blob): continue
            old_pos = struct.unpack_from('<H', new_blob, pos_off)[0]
            struct.pack_into('<H', new_blob, pos_off, (old_pos + rung_size) & 0xffff)
            if rung_ctr_off < len(new_blob):
                new_blob[rung_ctr_off] = (new_blob[rung_ctr_off] + 1) & 0xff
            break
        self.scr_raw[idx] = bytes(new_blob)

        # Also add entries to SC-NICK for each newly-referenced address.
        # Tags are split into a non-numeric prefix (e.g. "C") and a number
        # ("21"); this matches what EB Pro records for memory references.
        for tag in (tag_no, tag_out):
            type_str = ''.join(c for c in tag if not c.isdigit())
            num_str  = ''.join(c for c in tag if c.isdigit())
            if type_str and num_str:
                self._add_nick_entry(type_str, num_str)

    # ----- low-level encode -----

    def encode(self) -> bytes:
        """Re-encode the project back into a .ckp byte sequence."""
        # SC-PRJ start offset is byte 2 of the magic header (e.g. 0x54
        # for minimal.ckp, 0x5c for TestProject.ckp, 0x74 for variant A).
        prj_off = self.magic[2]

        # Section order matters for round-trip fidelity.
        #
        #   Variant B: PRJ, INI, NICK, DVIEW, SCR..., CMORE
        #   Variant A: PRJ, INI, DVIEW, SCR..., ZIP1, ZIP2, CMORE, ZIP3
        #
        # The variant-A interleaving with zips is what real EB Pro builds
        # produce.  We reproduce it so untouched files round-trip byte-
        # identical and any edited file is still a valid .ckp.
        ordered: list[tuple[str, bytes]] = [
            ('prj', self.prj_raw),
            ('ini', self.ini_raw),
        ]
        if self.variant == 'B':
            if self.nick_raw:
                ordered.append(('nick', self.nick_raw))
            ordered.append(('dview', self.dview_raw))
            for blob in self.scr_raw:
                ordered.append(('scr', blob))
            ordered.append(('cmore', self.cmore_raw))
        else:
            ordered.append(('dview', self.dview_raw))
            for blob in self.scr_raw:
                ordered.append(('scr', blob))
            for i, blob in enumerate(self.raw_zip_blobs[:2]):
                ordered.append(('zip', blob))
            ordered.append(('cmore', self.cmore_raw))
            if len(self.raw_zip_blobs) > 2:
                ordered.append(('zip', self.raw_zip_blobs[2]))

        # Compute layout offsets
        offsets: dict[str, list[tuple[int, int]]] = {}
        cursor = prj_off
        body = bytearray()
        for kind, blob in ordered:
            offsets.setdefault(kind, []).append((cursor, len(blob)))
            body.extend(blob)
            cursor += len(blob)
        zip_offsets = offsets.get('zip', [])

        # Build the section table
        out = bytearray(prj_off)
        out[:4] = self.magic
        out[4:6] = b'\x00\x00'
        prj_at, prj_sz = offsets['prj'][0]
        ini_at, ini_sz = offsets['ini'][0]
        dview_at, dview_sz = offsets['dview'][0]
        cmore_at, cmore_sz = offsets['cmore'][0]
        struct.pack_into('<I', out, 0x06, prj_sz)
        struct.pack_into('<I', out, 0x0a, ini_at)
        struct.pack_into('<I', out, 0x0e, ini_sz)
        if 'nick' in offsets:
            nick_at, nick_sz = offsets['nick'][0]
            struct.pack_into('<I', out, 0x12, nick_at)
            struct.pack_into('<I', out, 0x16, nick_sz)
        struct.pack_into('<I', out, 0x1a, dview_at)
        struct.pack_into('<I', out, 0x1e, dview_sz)
        struct.pack_into('<I', out, 0x22, cmore_at)
        struct.pack_into('<I', out, 0x26, cmore_sz)
        scr_locs = offsets.get('scr', [])
        struct.pack_into('<H', out, 0x2a, len(scr_locs))
        for i, (off, sz) in enumerate(scr_locs):
            struct.pack_into('<I', out, 0x2c + 8 * i, off)
            struct.pack_into('<I', out, 0x30 + 8 * i, sz)
        for i, (off, sz) in enumerate(zip_offsets):
            struct.pack_into('<I', out, 0x5c + 8 * i, off)
            struct.pack_into('<I', out, 0x60 + 8 * i, sz)

        # Stamp the magic checksum at bytes 0-1.  EB Pro computes this as
        # the XOR of all u16 LE words from offset 2 onwards.  Bytes 2-3 of
        # the magic header are preserved from the original file (they encode
        # the SC-PRJ start offset + a trailing 0).
        full = bytes(out) + bytes(body)
        magic = compute_magic(full)
        full = struct.pack('<H', magic) + full[2:]
        return full

    def save(self, path: str) -> None:
        with open(path, 'wb') as f:
            f.write(self.encode())


def compute_magic(file_bytes: bytes) -> int:
    """Compute the 16-bit XOR-checksum that EB Pro writes at file offset 0-1.

    Algorithm (from CLICK.exe FUN_004fe470):  XOR all u16 LE words from
    file offset 2 to end of file.  If file length is odd, the trailing
    byte is treated as if followed by a zero (i.e. read as a u16 LE).
    """
    out = 0
    pos = 2
    end = len(file_bytes)
    while pos < end:
        # Read up to 65535 bytes per chunk (matching the C++ original)
        chunk_end = min(pos + 0xffff, end)
        chunk = file_bytes[pos:chunk_end]
        # If odd length, pad with one zero byte
        if len(chunk) % 2:
            chunk = chunk + b'\x00'
        for i in range(0, len(chunk), 2):
            out ^= chunk[i] | (chunk[i+1] << 8)
        pos = chunk_end
    return out & 0xFFFF


# --- top-level decoder ----------------------------------------------------

_SECTION_MARKER = {'A': 0x0350, 'B': 0x0328}


def decode_ckp(data: bytes) -> CkpProject:
    if data[:4] == b'vPt\x00':
        variant = 'A'
    else:
        variant = 'B'
    magic = bytes(data[:4])
    section_marker = _SECTION_MARKER[variant]

    sub_count = u16(data, 0x2a)
    scr_slots = []
    for i in range(sub_count):
        sec_off = u32(data, 0x2c + 8 * i)
        sec_sz  = u32(data, 0x30 + 8 * i)
        if sec_off and sec_sz:
            scr_slots.append((sec_off, sec_sz))

    # SC-PRJ start offset is byte 2 of the magic header
    prj_off            = data[2]
    prj_sz             = u32(data, 0x06)
    ini_off,   ini_sz   = u32(data, 0x0a), u32(data, 0x0e)
    nick_off,  nick_sz  = u32(data, 0x12), u32(data, 0x16)
    dview_off, dview_sz = u32(data, 0x1a), u32(data, 0x1e)
    cmore_off, cmore_sz = u32(data, 0x22), u32(data, 0x26)

    ini_file, ini_body = parse_ini(data, ini_off, ini_sz)
    dview_files = parse_dview(data, dview_off, dview_sz)
    tags_csv = parse_cmore(data, cmore_off, cmore_sz)
    subroutines = [parse_subroutine(data, off, sz) for off, sz in scr_slots]
    nicknames = parse_nick(data, nick_off, nick_sz) if nick_sz else []

    raw_zip_blobs: list[bytes] = []
    if variant == 'A':
        for off_off, sz_off in [(0x5c, 0x60), (0x64, 0x68), (0x6c, 0x70)]:
            off, sz = u32(data, off_off), u32(data, sz_off)
            if off and sz:
                raw_zip_blobs.append(data[off:off+sz])

    return CkpProject(
        variant=variant,
        magic=magic,
        section_marker=section_marker,
        prj_raw=data[prj_off:prj_off+prj_sz],
        ini_raw=data[ini_off:ini_off+ini_sz],
        nick_raw=data[nick_off:nick_off+nick_sz] if nick_sz else b'',
        dview_raw=data[dview_off:dview_off+dview_sz],
        cmore_raw=data[cmore_off:cmore_off+cmore_sz],
        scr_raw=[data[off:off+sz] for off, sz in scr_slots],
        raw_zip_blobs=raw_zip_blobs,
        ini_file=ini_file,
        ini_body=ini_body,
        dview_files=dview_files,
        tags_csv=tags_csv,
        subroutines=subroutines,
        nicknames=nicknames,
    )


# --- CLI ------------------------------------------------------------------

def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__); return 1
    ckp_path = argv[1]
    out_dir = argv[2] if len(argv) > 2 else os.path.splitext(ckp_path)[0] + '.extracted'
    with open(ckp_path, 'rb') as f:
        data = f.read()
    project = decode_ckp(data)
    project.write(out_dir)

    print(f"Decoded {ckp_path}  (variant {project.variant})")
    print(f"  -> {out_dir}/")
    print(f"     {project.ini_file:<20} ({len(project.ini_body)} B)")
    print(f"     DataView.list      ({len(project.dview_files)} files)")
    print(f"     tags.csv           ({project.tags_csv.count(chr(10))} lines)")
    print(f"     nicknames.csv      ({len(project.nicknames)} entries)")
    rung_count = sum(len(s.rungs) for s in project.subroutines)
    print(f"     program.txt        ({len(project.subroutines)} subs, "
          f"{rung_count} rungs total)")
    for i, _ in enumerate(project.raw_zip_blobs, 1):
        print(f"     zip{i}.zip           (variant A trailing zip blob)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
