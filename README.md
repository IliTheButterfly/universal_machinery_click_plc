# click_plc

Read and write AutomationDirect [CLICK PLC](https://www.automationdirect.com/click/) project files (`.ckp`) from Python.

CLICK Programming Software stores projects in an undocumented binary container.  This package decodes that container and re-encodes it byte-for-byte, including the file's XOR-16 integrity checksum and the SC-NICK / SC-SCR section bookkeeping that EB Pro updates per save.  Edited files load cleanly in CLICK Programming Software v3.43.

This package is consumed as a git submodule by the [universal_machinery](https://github.com/iliana/universal_machinery) project but works fine standalone.

## Status

| Capability | State |
|---|---|
| Read both container variants (older `vPt` magic, newer 4-byte hash magic) | ✅ |
| Round-trip an unedited project byte-identical | ✅ |
| Insert ladder rungs of common shapes | ✅ |
| Add new subroutines (clone an existing one) | ✅ |
| Decode every section (SC-PRJ, SC-INI, SC-NICK, SC-DVIEW, SC-CMORE, SC-SCR) | ✅ |
| Build a rung from instruction primitives without a template | ❌ (uses verbatim templates extracted from real EB Pro output) |
| Cold-build SC-SCR for a brand-new subroutine | ❌ (clone an existing one instead) |
| Tag-length flexibility | partial — each `Rung.<helper>` requires specific tag lengths matching its template |

## Install

From source (this directory):

```sh
pip install -e .
```

## Usage

```python
from click_plc import decode_ckp, Rung

project = decode_ckp(open('Project.ckp', 'rb').read())

# Inspect the program
print(project.render_program())

# Add ladder rungs (each is inserted before the subroutine's terminator)
project.add_rung(sub_id=2, rung=Rung.no_out("C40", "C41"))                # NO + Out
project.add_rung(sub_id=2, rung=Rung.no_nc_out("C50", "X005", "C051"))    # NO + NC + Out
project.add_rung(sub_id=2, rung=Rung.copy("DS30", "DS31"))                # Copy register
project.add_rung(sub_id=1, rung=Rung.call("Sub3"))                        # Call subroutine

# Add a new subroutine (cloned from existing; name length must match)
new_id = project.add_subroutine_clone(source_sub_id=2, new_name="Sub3")

# Save
project.save('out.ckp')
```

The `encode()` step recomputes the file's XOR-16 magic, adds new SC-NICK entries for every newly-referenced address, increments the section's rung-counter byte at offset 0x8f, and updates the terminator instruction's position word and rung-counter byte.

## CLI

```sh
ckp-extract Project.ckp out_dir/
```

Dumps a CKP into:
- `Project.ini` — the embedded system config text
- `tags.csv` — the SC-CMORE tag dictionary
- `program.txt` — the decoded ladder logic
- `nicknames.csv` — the SC-NICK user-defined names
- `DataView.list` — the data view filenames
- `zip{1,2,3}.zip` — variant-A trailing zip blobs (older format)

## Format notes

The file's container layout, magic algorithm, and rung-byte encodings are documented in source-code comments inside `click_plc/ckp_decoder.py`.  Highlights:

- **Magic** is `XOR16(file[2:])` — byte 2 holds the SC-PRJ start offset, byte 3 is always zero.
- **Sections** are at fixed offsets in a section table at file start; each section body begins with a 0x40-byte header.
- **Ladder logic** is a stream of length-prefixed UTF-16LE name strings (`ContactNO`, `Out`, `Copy`, `Call`, `Return`, `End`) followed by an opcode byte, a 16-byte metadata block, and zero-or-more memory-tag pstrs.  Rungs are delimited by a 66-byte bitmap.
- **Position words** are sequential u16 ids assigned per instruction; the encoder allocates `max_existing + 0x10` for each new instruction and preserves the template's relative spacing.
- **Project.ini `wsep`/`wseu`/`emsep`/`emseu`** are 17-digit integers that EB Pro rotates per save — they don't appear to affect file validity, so this package leaves them untouched.

## License

GPL-3.0-or-later, same as the parent `universal_machinery` project.
