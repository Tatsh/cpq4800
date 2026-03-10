#!/usr/bin/env python3
"""Patch a Compaq Presario 4800 QuickRestore ISO to accept any serial number.

Applies 6 binary patches to QR.EXE and comments out the Compaq hardware check
in CPQR.BAT. See serial-number.md for full documentation.

Requirements: deark, upx, mtools (mcopy/mdel), mkisofs, isoinfo
"""
import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile

log = logging.getLogger(__name__)

PATCHES = (
    # (offset, expected, replacement, description)
    (0x4B84, bytes([0x74]), bytes([0xEB]),
     "Patch 1: ManufMode — skip SkuNumber abort (JZ -> JMP)"),
    (0x4BAF, bytes([0x75, 0x03]), bytes([0x90, 0x90]),
     "Patch 2: ManufMode — skip BOMID mismatch abort (JNZ -> NOP NOP)"),
    (0x5965, bytes([0x74]), bytes([0xEB]),
     "Patch 3: MAIN do-while — force first BOM table match (JZ -> JMP)"),
    (0x5984, bytes([0x74, 0x03]), bytes([0x90, 0x90]),
     "Patch 4: MAIN do-while — never show 'not supported' (JZ -> NOP NOP)"),
    (0x59BB, bytes([0x75]), bytes([0xEB]),
     "Patch 5: MAIN do-while — always exit loop (JNZ -> JMP)"),
    (0x08EC9, bytes([0x74]), bytes([0xEB]),
     "Patch 6: FUN_14f4_02e6 — force CSV serial match (JZ -> JMP)"),
)

REQUIRED_TOOLS = ("deark", "upx", "mattrib", "mcopy", "mdel", "mkisofs",
                  "isoinfo")
VOLUME_ID = "WAN05921Q8"


def run(cmd, **kwargs):
    kwargs.setdefault("check", True)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(cmd, **kwargs)


def extract_iso(iso_path, dest):
    """Extract all files from an ISO using isoinfo LBA offsets."""
    result = run(["isoinfo", "-i", iso_path, "-l"], text=True, timeout=120)
    text = result.stdout + result.stderr

    current_dir = "/"
    entries = []
    pat = re.compile(r'([d-])r.x......\s+\d+\s+\d+\s+\d+\s+(\d+)\s+'
                     r'\w+\s+\d+\s+\d+\s+'
                     r'\[\s*(\d+)\s+\d+\]\s+(.+)$')
    for line in text.splitlines():
        line_s = line.strip()
        if line_s.startswith("Directory listing of "):
            current_dir = line_s.split("Directory listing of ")[1].strip()
            if not current_dir.endswith("/"):
                current_dir += "/"
            continue
        m = pat.search(line_s)
        if not m:
            continue
        is_dir = m.group(1) == 'd'
        size, lba = int(m.group(2)), int(m.group(3))
        name = m.group(4).strip()
        if name.endswith(";1"):
            name = name[:-2]
        if name in (".", ".."):
            continue
        entries.append((is_dir, current_dir + name, size, lba))

    os.makedirs(dest, exist_ok=True)
    count = 0
    with open(iso_path, "rb") as iso:
        for is_dir, path, size, lba in entries:
            full = os.path.join(dest, path.lstrip("/"))
            if is_dir:
                os.makedirs(full, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(full), exist_ok=True)
                iso.seek(lba * 2048)
                data = iso.read(size)
                with open(full, "wb") as f:
                    f.write(data)
                count += 1
    return count


def unpack_pklite(exe_path, out_path):
    """Unpack a PKLITE-compressed EXE using deark."""
    out_dir = os.path.dirname(out_path) or "."
    result = run(["deark", "-m", "pklite", "-od", out_dir, exe_path],
                 text=True)
    deark_out = os.path.join(out_dir, "output.000.exe")
    if not os.path.exists(deark_out):
        raise RuntimeError(
            f"deark did not produce output file\n{result.stdout}\n{result.stderr}"
        )
    os.rename(deark_out, out_path)


def apply_patches(exe_path):
    """Apply the 6 binary patches to an unpacked QRU.EXE."""
    with open(exe_path, "r+b") as f:
        data = bytearray(f.read())
        for offset, expected, replacement, desc in PATCHES:
            length = len(expected)
            actual = bytes(data[offset:offset + length])
            if actual != expected:
                raise ValueError(
                    f"{desc}: expected {expected.hex()} at 0x{offset:05X}, "
                    f"found {actual.hex()}")
            data[offset:offset + length] = replacement
            log.info("  %s", desc)
        f.seek(0)
        f.write(data)
        f.truncate()


def compress_upx(in_path, out_path):
    """Compress an EXE with UPX for 8086."""
    run(["upx", "--best", "--8086", "-o", out_path, in_path])


def patch_cpqr_bat(bootsect_path):
    """Comment out CPQZ.EXE in CPQR.BAT inside the floppy image."""
    result = run(["mcopy", "-i", bootsect_path, "::TOOLS/CPQR.BAT", "-"],
                 capture_output=True)
    bat = result.stdout
    original_line = b"\\TOOLS\\CPQZ.EXE"
    patched_line = b"REM \\TOOLS\\CPQZ.EXE"
    if patched_line in bat:
        log.info("  CPQR.BAT already patched")
        return
    if original_line not in bat:
        log.warning("CPQZ.EXE line not found in CPQR.BAT")
        return
    bat = bat.replace(original_line, patched_line, 1)
    with tempfile.NamedTemporaryFile(suffix=".BAT", delete=False) as tmp:
        tmp.write(bat)
        tmp_path = tmp.name
    try:
        run([
            "mattrib", "-r", "-s", "-h", "-i", bootsect_path,
            "::TOOLS/CPQR.BAT"
        ])
        run(["mdel", "-i", bootsect_path, "::TOOLS/CPQR.BAT"])
        run(["mcopy", "-i", bootsect_path, tmp_path, "::TOOLS/CPQR.BAT"])
    finally:
        os.unlink(tmp_path)
    log.info("  Commented out CPQZ.EXE in CPQR.BAT")


def replace_qr_exe(bootsect_path, new_qr_path):
    """Replace QR.EXE inside a BOOTSECT.BIN floppy image."""
    run(["mattrib", "-r", "-s", "-h", "-i", bootsect_path, "::QR.EXE"])
    run(["mdel", "-i", bootsect_path, "::QR.EXE"])
    run(["mcopy", "-i", bootsect_path, new_qr_path, "::QR.EXE"])


def build_iso(source_dir, output_path):
    """Build the patched ISO with El Torito floppy emulation boot."""
    run([
        "mkisofs",
        "-V",
        VOLUME_ID,
        "-iso-level",
        "4",
        "-b",
        "BOOTSECT.BIN",
        "-c",
        "BOOTCAT.BIN",
        "-o",
        output_path,
        source_dir,
    ],
        text=True)


def main():
    logging.basicConfig(format="%(message)s", level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Patch a Compaq Presario 4800 QuickRestore ISO to accept "
        "any serial number.")
    parser.add_argument("input_iso", help="Path to the original ISO")
    parser.add_argument("-o",
                        "--output",
                        help="Output ISO path "
                        "(default: <input>-patched.iso)")
    args = parser.parse_args()

    if not os.path.isfile(args.input_iso):
        log.error("%s not found", args.input_iso)
        sys.exit(1)

    if args.output:
        output_iso = args.output
    else:
        base, ext = os.path.splitext(args.input_iso)
        output_iso = f"{base}-patched{ext}"

    missing = [t for t in REQUIRED_TOOLS if not shutil.which(t)]
    if missing:
        log.error("Required tools not found in PATH: %s", ", ".join(missing))
        sys.exit(1)

    with tempfile.TemporaryDirectory(prefix="cpq4800-patch-") as tmpdir:
        iso_dir = os.path.join(tmpdir, "iso")
        qr_dir = os.path.join(tmpdir, "qr")
        os.makedirs(qr_dir)

        log.info("Extracting %s...", args.input_iso)
        count = extract_iso(args.input_iso, iso_dir)
        log.info("  %d files extracted", count)

        bootsect = os.path.join(iso_dir, "BOOTSECT.BIN")
        if not os.path.isfile(bootsect):
            log.error("BOOTSECT.BIN not found in ISO")
            sys.exit(1)

        log.info("Extracting QR.EXE from BOOTSECT.BIN...")
        qr_orig = os.path.join(qr_dir, "QR.EXE")
        run(["mcopy", "-i", bootsect, "::QR.EXE", qr_orig])

        log.info("Unpacking PKLITE-compressed QR.EXE...")
        qru = os.path.join(qr_dir, "QRU.EXE")
        unpack_pklite(qr_orig, qru)
        log.info("  Unpacked: %d bytes", os.path.getsize(qru))

        log.info("Applying binary patches...")
        apply_patches(qru)

        log.info("Recompressing with UPX...")
        qr_upx = os.path.join(qr_dir, "QR_UPX.EXE")
        compress_upx(qru, qr_upx)
        log.info("  Compressed: %d bytes", os.path.getsize(qr_upx))

        log.info("Replacing QR.EXE in BOOTSECT.BIN...")
        replace_qr_exe(bootsect, qr_upx)

        log.info("Patching CPQR.BAT in BOOTSECT.BIN...")
        patch_cpqr_bat(bootsect)

        log.info("Building %s...", output_iso)
        build_iso(iso_dir, output_iso)

    size_mb = os.path.getsize(output_iso) / (1024 * 1024)
    log.info("Done: %s (%.1f MB)", output_iso, size_mb)


if __name__ == "__main__":
    main()
