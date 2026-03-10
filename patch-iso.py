#!/usr/bin/env python
"""
Patch a Compaq Presario 4800 QuickRestore ISO to accept any serial number.

Applies 6 binary patches to QR.EXE and comments out the Compaq hardware check in CPQR.BAT.

Requirements: deark, upx, mtools (mcopy/mdel), mkisofs, isoinfo.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
import argparse
import logging
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

if TYPE_CHECKING:
    from collections.abc import Sequence

log = logging.getLogger(__name__)

PATCHES = (
    (0x4B84, bytes([0x74]), bytes([0xEB]), 'Patch 1: ManufMode — skip SkuNumber abort (JZ -> JMP)'),
    (0x4BAF, bytes([0x75, 0x03]), bytes(
        [0x90, 0x90]), 'Patch 2: ManufMode — skip BOMID mismatch abort (JNZ -> NOP NOP)'),
    (0x5965, bytes([0x74]), bytes(
        [0xEB]), 'Patch 3: MAIN do-while — force first BOM table match (JZ -> JMP)'),
    (0x5984, bytes([0x74, 0x03]), bytes(
        [0x90, 0x90]), "Patch 4: MAIN do-while — never show 'not supported' (JZ -> NOP NOP)"),
    (0x59BB, bytes([0x75]), bytes(
        [0xEB]), 'Patch 5: MAIN do-while — always exit loop (JNZ -> JMP)'),
    (0x08EC9, bytes([0x74]), bytes(
        [0xEB]), 'Patch 6: FUN_14f4_02e6 — force CSV serial match (JZ -> JMP)'),
)
REQUIRED_TOOLS = ('deark', 'upx', 'mattrib', 'mcopy', 'mdel', 'mkisofs', 'isoinfo')
VOLUME_ID = 'WAN05921Q8'


def _run(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    kwargs.setdefault('check', True)
    kwargs.setdefault('capture_output', True)
    kwargs.setdefault('stdin', subprocess.DEVNULL)
    return subprocess.run(cmd, **kwargs)  # noqa: PLW1510


def _extract_iso(iso_path: str, dest: str) -> int:
    result = _run(['isoinfo', '-i', iso_path, '-l'], text=True, timeout=120)
    text = result.stdout + result.stderr

    current_dir = '/'
    entries = []
    pat = re.compile(r'([d-])r.x......\s+\d+\s+\d+\s+\d+\s+(\d+)\s+'
                     r'\w+\s+\d+\s+\d+\s+'
                     r'\[\s*(\d+)\s+\d+\]\s+(.+)$')
    for line in text.splitlines():
        line_s = line.strip()
        if line_s.startswith('Directory listing of '):
            current_dir = line_s.split('Directory listing of ')[1].strip()
            if not current_dir.endswith('/'):
                current_dir += '/'
            continue
        m = pat.search(line_s)
        if not m:
            continue
        is_dir = m.group(1) == 'd'
        size, lba = int(m.group(2)), int(m.group(3))
        name = m.group(4).strip()
        name = name.removesuffix(';1')
        if name in {'.', '..'}:
            continue
        entries.append((is_dir, current_dir + name, size, lba))

    pathlib.Path(dest).mkdir(exist_ok=True, parents=True)
    count = 0
    with pathlib.Path(iso_path).open('rb') as iso:
        for is_dir, path, size, lba in entries:
            full = pathlib.Path(dest) / path.lstrip('/')
            if is_dir:
                pathlib.Path(full).mkdir(exist_ok=True, parents=True)
            else:
                pathlib.Path(pathlib.Path(full).parent).mkdir(exist_ok=True, parents=True)
                iso.seek(lba * 2048)
                data = iso.read(size)
                pathlib.Path(full).write_bytes(data)
                count += 1
    return count


def _unpack_pklite(exe_path: str, out_path: str) -> None:
    out_dir = pathlib.Path(out_path).parent or '.'
    result = _run(('deark', '-m', 'pklite', '-od', str(out_dir), exe_path), text=True)
    deark_out = pathlib.Path(out_dir) / 'output.000.exe'
    if not deark_out.exists():
        msg = f'deark did not produce output file\n{result.stdout}\n{result.stderr}'
        raise RuntimeError(msg)
    pathlib.Path(deark_out).rename(out_path)


def _apply_patches(exe_path: str) -> None:
    with pathlib.Path(exe_path).open('r+b') as f:
        data = bytearray(f.read())
        for offset, expected, replacement, desc in PATCHES:
            length = len(expected)
            actual = bytes(data[offset:offset + length])
            if actual != expected:
                msg = f'{desc}: expected {expected.hex()} at 0x{offset:05X}, found {actual.hex()}'
                raise ValueError(msg)
            data[offset:offset + length] = replacement
            log.info('  %s', desc)
        f.seek(0)
        f.write(data)
        f.truncate()


def _compress_upx(in_path: str, out_path: str) -> None:
    _run(('upx', '--best', '--8086', '-o', out_path, in_path))


def _patch_cpqr_bat(bootsect_path: str) -> None:
    result = cast(
        'subprocess.CompletedProcess[bytes]',
        _run(('mcopy', '-i', bootsect_path, '::TOOLS/CPQR.BAT', '-'), capture_output=True))
    bat = result.stdout
    original_line = b'\\TOOLS\\CPQZ.EXE'
    patched_line = b'REM \\TOOLS\\CPQZ.EXE'
    if patched_line in bat:
        log.info('  CPQR.BAT already patched')
        return
    if original_line not in bat:
        log.warning('CPQZ.EXE line not found in CPQR.BAT')
        return
    bat = bat.replace(original_line, patched_line, 1)
    with tempfile.NamedTemporaryFile(suffix='.BAT', delete=False) as tmp:
        tmp.write(bat)
        tmp_path = tmp.name
    try:
        _run(('mattrib', '-r', '-s', '-h', '-i', bootsect_path, '::TOOLS/CPQR.BAT'))
        _run(('mdel', '-i', bootsect_path, '::TOOLS/CPQR.BAT'))
        _run(('mcopy', '-i', bootsect_path, tmp_path, '::TOOLS/CPQR.BAT'))
    finally:
        pathlib.Path(tmp_path).unlink()
    log.info('  Commented out CPQZ.EXE in CPQR.BAT')


def _replace_qr_exe(bootsect_path: str, new_qr_path: str) -> None:
    _run(('mattrib', '-r', '-s', '-h', '-i', bootsect_path, '::QR.EXE'))
    _run(('mdel', '-i', bootsect_path, '::QR.EXE'))
    _run(('mcopy', '-i', bootsect_path, new_qr_path, '::QR.EXE'))


def _build_iso(source_dir: str, output_path: str) -> None:
    _run(('mkisofs', '-V', VOLUME_ID, '-iso-level', '4', '-b', 'BOOTSECT.BIN', '-c', 'BOOTCAT.BIN',
          '-o', output_path, source_dir),
         text=True)


def main() -> int:
    """Patch an ISO."""
    logging.basicConfig(format='%(message)s', level=logging.INFO)

    parser = argparse.ArgumentParser(
        description='Patch a Compaq Presario 4800 QuickRestore ISO to accept any serial number.')
    parser.add_argument('input_iso', help='Path to the original ISO')
    parser.add_argument('-o', '--output', help='Output ISO path (default: <input>-patched.iso)')
    args = parser.parse_args()

    if not pathlib.Path(args.input_iso).is_file():
        log.error('%s not found', args.input_iso)
        return 1

    if args.output:
        output_iso = args.output
    else:
        input_path = pathlib.Path(args.input_iso)
        output_iso = str(input_path.parent / f'{input_path.stem}-patched{input_path.suffix}')

    missing = [t for t in REQUIRED_TOOLS if not shutil.which(t)]
    if missing:
        log.error('Required tools not found in PATH: %s', ', '.join(missing))
        return 1

    with tempfile.TemporaryDirectory(prefix='cpq4800-patch-') as tmpdir:
        iso_dir = pathlib.Path(tmpdir) / 'iso'
        qr_dir = pathlib.Path(tmpdir) / 'qr'
        pathlib.Path(qr_dir).mkdir(parents=True)

        log.info('Extracting %s...', args.input_iso)
        count = _extract_iso(args.input_iso, str(iso_dir))
        log.info('  %d files extracted', count)

        bootsect = iso_dir / 'BOOTSECT.BIN'
        if not pathlib.Path(bootsect).is_file():
            log.error('BOOTSECT.BIN not found in ISO')
            return 1

        log.info('Extracting QR.EXE from BOOTSECT.BIN...')
        qr_orig = qr_dir / 'QR.EXE'
        _run(('mcopy', '-i', str(bootsect), '::QR.EXE', str(qr_orig)))

        log.info('Unpacking PKLITE-compressed QR.EXE...')
        qru = qr_dir / 'QRU.EXE'
        _unpack_pklite(str(qr_orig), str(qru))
        log.info('  Unpacked: %d bytes', pathlib.Path(qru).stat().st_size)

        log.info('Applying binary patches...')
        _apply_patches(str(qru))

        log.info('Recompressing with UPX...')
        qr_upx = qr_dir / 'QR_UPX.EXE'
        _compress_upx(str(qru), str(qr_upx))
        log.info('  Compressed: %d bytes', pathlib.Path(qr_upx).stat().st_size)

        log.info('Replacing QR.EXE in BOOTSECT.BIN...')
        _replace_qr_exe(str(bootsect), str(qr_upx))

        log.info('Patching CPQR.BAT in BOOTSECT.BIN...')
        _patch_cpqr_bat(str(bootsect))

        log.info('Building %s...', output_iso)
        _build_iso(str(iso_dir), output_iso)

    size_mb = pathlib.Path(output_iso).stat().st_size / (1024 * 1024)
    log.info('Done: %s (%.1f MB)', output_iso, size_mb)

    return 0


if __name__ == '__main__':
    sys.exit(main())
