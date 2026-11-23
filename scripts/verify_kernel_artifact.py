#!/usr/bin/env python3
"""Verify compiled kernel .config, Image.gz banner safety, and mt6785.dtb magic."""

from __future__ import annotations

import argparse
import gzip
import re
import struct
import sys
from pathlib import Path
from typing import Dict, List

BANNED_BANNER_SUBSTRINGS = (
    "lineage",
    "ksu",
    "kernelsu",
    "susfs",
    "dirty",
    "zenith",
    "fv-az",
    "runner",
)

EXPECTED_TRACK_META = {
    "4.19-r25p0": {
        "kver_prefix": "4.19.",
        "gpu_version": '"mali bifrost r25p0"',
        "require_xdp": True,
    },
    "4.19-r32p1": {
        "kver_prefix": "4.19.",
        "gpu_version": '"mali valhall r32p1"',
        "require_xdp": True,
    },
    "4.14-r25p0": {
        "kver_prefix": "4.14.",
        "gpu_version": '"mali bifrost r25p0"',
        "require_xdp": False,
    },
}

REQUIRED_Y_SYMBOLS = (
    "CONFIG_EROFS_FS",
    "CONFIG_EROFS_FS_XATTR",
    "CONFIG_EROFS_FS_SECURITY",
    "CONFIG_EROFS_FS_ZIP",
    "CONFIG_USERFAULTFD",
    "CONFIG_BPF_SYSCALL",
    "CONFIG_CGROUP_BPF",
    "CONFIG_BLK_INLINE_ENCRYPTION",
    "CONFIG_SCSI_UFS_CRYPTO",
    "CONFIG_FS_ENCRYPTION_INLINE_CRYPT",
    "CONFIG_F2FS_FS_ENCRYPTION",
    "CONFIG_OVERLAY_FS",
    "CONFIG_MICROTRUST_TEE_SUPPORT",
    "CONFIG_SECURITY_SELINUX",
    "CONFIG_EXT4_FS",
    "CONFIG_FS_VERITY",
)


MAX_IMAGE_GZ_BYTES = 15 * 1024 * 1024  # Safely below MT6785 LK 16 MiB (0x01000000) zImage limit
REQUIRED_DTB_SYMBOLS = ("swtp", "swtp_1")


def extract_fdt_symbols(fdt: bytes) -> set[str]:
    if len(fdt) < 40:
        raise ValueError("FDT too small")
    magic, _totalsize, off_struct, off_strings, _off_mem, _ver, _lver, _cpu, sz_str, sz_struct = struct.unpack(
        ">10I", fdt[:40]
    )
    if magic != 0xD00DFEED:
        raise ValueError(f"Invalid FDT magic: 0x{magic:08x}")
    strtab = fdt[off_strings : off_strings + sz_str]
    pos = off_struct
    end = off_struct + sz_struct
    stack: List[str] = []
    symbols: set[str] = set()
    while pos < end:
        tok = struct.unpack(">I", fdt[pos : pos + 4])[0]
        pos += 4
        if tok == 1:  # FDT_BEGIN_NODE
            nul = fdt.find(b"\x00", pos)
            name = fdt[pos:nul].decode("ascii", "replace")
            pos = (nul + 4) & ~3
            stack.append(name)
        elif tok == 2:  # FDT_END_NODE
            stack.pop()
        elif tok == 3:  # FDT_PROP
            plen, nameoff = struct.unpack(">2I", fdt[pos : pos + 8])
            pos += 8
            pos = (pos + plen + 3) & ~3
            path = "/" + "/".join(s for s in stack if s)
            if path == "/__symbols__":
                pname = strtab[nameoff : strtab.find(b"\x00", nameoff)].decode("ascii", "replace")
                symbols.add(pname)
        elif tok == 9:  # FDT_END
            break
    return symbols


def parse_kconfig(config_path: Path) -> Dict[str, str]:
    vals: Dict[str, str] = {}
    for raw in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# ") and line.endswith(" is not set"):
            sym = line[2 : -len(" is not set")].strip()
            vals[sym] = "n"
        elif not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals


def verify_artifacts(out_dir: Path, track: str) -> None:
    if track not in EXPECTED_TRACK_META:
        raise ValueError(f"Unsupported track: {track!r}")
    meta = EXPECTED_TRACK_META[track]

    cfg_path = out_dir / ".config"
    img_gz_path = out_dir / "arch/arm64/boot/Image.gz"
    dtb_path = out_dir / "arch/arm64/boot/dts/mediatek/mt6785.dtb"

    for p in (cfg_path, img_gz_path, dtb_path):
        if not p.is_file():
            raise FileNotFoundError(f"Missing expected build artifact: {p}")

    cfg = parse_kconfig(cfg_path)
    errors: List[str] = []

    for sym in REQUIRED_Y_SYMBOLS:
        if cfg.get(sym) != "y":
            errors.append(f"{sym}={cfg.get(sym)!r} (expected 'y')")

    if meta["require_xdp"] and cfg.get("CONFIG_XDP_SOCKETS") != "y":
        errors.append(f"CONFIG_XDP_SOCKETS={cfg.get('CONFIG_XDP_SOCKETS')!r} (expected 'y')")

    if cfg.get("CONFIG_LOCALVERSION") != '"-perf"':
        errors.append(f"CONFIG_LOCALVERSION={cfg.get('CONFIG_LOCALVERSION')!r} (expected '\"-perf\"')")
    if cfg.get("CONFIG_LOCALVERSION_AUTO", "n") != "n":
        errors.append(f"CONFIG_LOCALVERSION_AUTO={cfg.get('CONFIG_LOCALVERSION_AUTO')!r} (expected unset/'n')")
    if cfg.get("CONFIG_MTK_GPU_VERSION") != meta["gpu_version"]:
        errors.append(
            f"CONFIG_MTK_GPU_VERSION={cfg.get('CONFIG_MTK_GPU_VERSION')!r} (expected {meta['gpu_version']!r})"
        )

    # Check Image.gz size (must stay below MT6785 LK 16 MiB zImage limit) and banner
    img_gz_bytes = img_gz_path.read_bytes()
    if len(img_gz_bytes) > MAX_IMAGE_GZ_BYTES:
        errors.append(
            f"Image.gz size {len(img_gz_bytes):,} B exceeds {MAX_IMAGE_GZ_BYTES:,} B LK zImage safety cap "
            "(ensure CONFIG_IKHEADERS / CONFIG_KALLSYMS_ALL are disabled)"
        )
    raw_image = gzip.decompress(img_gz_bytes)
    m = re.search(rb"Linux version ([^\x00\r\n]+)", raw_image)
    if not m:
        errors.append("Could not locate 'Linux version ...' banner inside decompressed Image.gz")
    else:
        banner = m.group(0).decode("utf-8", "replace")
        rel_match = re.match(r"Linux version (\S+)", banner)
        rel = rel_match.group(1) if rel_match else ""
        if not rel.startswith(str(meta["kver_prefix"])) or not rel.endswith("-perf"):
            errors.append(f"Unexpected kernel release '{rel}' in banner: {banner}")
        low = banner.lower()
        for bad in BANNED_BANNER_SUBSTRINGS:
            if bad in low:
                errors.append(f"Banned anti-tamper substring '{bad}' in banner: {banner}")
        print(f"[INFO] Kernel banner: {banner}")

    # Check mt6785.dtb magic and stock dtbo_a overlay symbol compatibility (swtp, swtp_1)
    dtb_bytes = dtb_path.read_bytes()
    if len(dtb_bytes) < 40 or struct.unpack(">I", dtb_bytes[:4])[0] != 0xD00DFEED:
        errors.append(f"Invalid FDT magic in {dtb_path}: {dtb_bytes[:4].hex()}")
    else:
        syms = extract_fdt_symbols(dtb_bytes)
        missing_syms = [s for s in REQUIRED_DTB_SYMBOLS if s not in syms]
        if missing_syms:
            errors.append(f"mt6785.dtb missing stock dtbo_a overlay __symbols__: {missing_syms}")

    if errors:
        raise AssertionError("Kernel artifact verification failed:\n  - " + "\n  - ".join(errors))

    print(
        f"[PASS] Verified {track}: Image.gz ({img_gz_path.stat().st_size:,} B), "
        f"mt6785.dtb ({dtb_path.stat().st_size:,} B), all required Kconfig flags OK."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify compiled Rosemary kernel artifacts")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--track", choices=tuple(EXPECTED_TRACK_META.keys()), required=True)
    args = parser.parse_args()
    verify_artifacts(args.out_dir, args.track)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
