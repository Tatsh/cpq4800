# Compaq Presario 4800 Quick Restore CD

Reverse engineering documentation and tools for the Compaq Presario 4800
Quick Restore CD (volume `WAN05921Q8`, August 1997). This CD shipped with
Presario 4808 and 4816 models and restores a factory Windows 95 installation.

## Background

The Quick Restore CD boots a minimal DOS environment from a 1.44 MB FAT12
floppy image (`BOOTSECT.BIN`) embedded as an El Torito boot record. The
restore process is orchestrated by two executables:

- **`QR.EXE`** -- the main program handling language selection, serial number
  entry, model verification, and launching the overlay.
- **`QROVL.EXE`** -- the overlay that interprets `.SCP` script files to
  partition/format the hard disk and extract password-protected ZIP archives
  containing the Windows 95 image.

Three layers of protection prevent the CD from being used on non-Compaq
hardware:

1. **Compaq hardware check** (`CPQZ.EXE`) -- verifies the machine is a
   genuine Compaq via a proprietary INT 15h BIOS call or SMBIOS Type 1
   manufacturer string matching.
2. **Serial number validation** -- requires a 12-character alphanumeric serial
   and cross-references it against a BOM (Bill of Materials) table.
3. **UIA-to-BOMID matching** -- compares identity data written to hidden disk
   sectors at the factory (`UIABOMWR.EXE`) against the CD's `BOMID.TXT`
   configuration.

## Documentation

See the [Wiki](https://github.com/Tatsh/cpq4800/wiki).

## Tools

[`patch-iso.py`](patch-iso.py) automates the entire patching process. Given
an original ISO, it:

1. Extracts the ISO contents using `isoinfo` LBA offsets
2. Extracts `QR.EXE` from `BOOTSECT.BIN` and unpacks it with `deark`
3. Applies six binary patches to bypass serial/BOMID validation
4. Recompresses with UPX and replaces `QR.EXE` in the floppy image
5. Comments out `CPQZ.EXE` in `CPQR.BAT` to skip the hardware check
6. Rebuilds the ISO with correct El Torito floppy-emulation boot

```shell
python3 patch-iso.py "Compaq Presario 4800_WAN05921Q8.bak.iso" -o patched.iso
```

Requirements: `deark`, `upx`, `mtools` (`mcopy`/`mdel`/`mattrib`), `mkisofs`,
`isoinfo`.

## Key Findings

- The ZIP password for the recovery archives is **`SMDCONSUMER`**, encoded in
  `.SCP` script files using a three-step scheme: length prefix in the low 5
  bits of the first byte, XOR each payload byte with `0x03`, then reverse.
- The serial number screen accepts any 12-character alphanumeric string -- the
  real access control is the UIA sector written at the factory, not the serial
  itself.
- All protection-relevant executables are compressed with either PKLITE
  (`QR.EXE`, `CPQZ.EXE`) or DIET (`UIABOMWR.EXE`) and must be unpacked
  before patching.

## ISO Details

| Field            | Value                                                         |
| ---------------- | ------------------------------------------------------------- |
| Volume ID        | `WAN05921Q8`                                                  |
| Format           | ISO 9660:1999 (level 4), El Torito                            |
| Boot media       | 1.44 MB floppy emulation (`BOOTSECT.BIN`)                     |
| Supported models | Presario 4808, Presario 4816                                  |
| OS               | Windows 95 OEM                                                |
| Archive          | [Internet Archive](https://archive.org/details/wan-05921-q-8) |
