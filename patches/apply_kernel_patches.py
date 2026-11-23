#!/usr/bin/env python3
"""Apply deterministic, idempotent kernel source patches before compilation."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

VALID_TRACKS = ("4.19-r25p0", "4.19-r32p1", "4.14-r25p0")


def _clean_version_markers(kernel_dir: Path) -> None:
    mk = kernel_dir / "Makefile"
    text = mk.read_text(encoding="utf-8")
    new_text, count = re.subn(r"(?m)^EXTRAVERSION\s*=.*$", "EXTRAVERSION =", text, count=1)
    if count != 1:
        raise RuntimeError(f"Could not find EXTRAVERSION line in {mk}")
    if new_text != text:
        mk.write_text(new_text, encoding="utf-8")
        print(f"[PATCH] Cleaned EXTRAVERSION in {mk}")

    for lv in sorted(kernel_dir.glob("localversion*")):
        if lv.is_file():
            lv.unlink()
            print(f"[PATCH] Removed {lv.name} from {kernel_dir}")

    scm = kernel_dir / ".scmversion"
    if not scm.exists() or scm.read_text(encoding="utf-8") != "":
        scm.write_text("", encoding="utf-8")
        print(f"[PATCH] Wrote empty .scmversion in {kernel_dir}")


def _patch_419_mali_r25p0(kernel_dir: Path) -> None:
    midgard = kernel_dir / "drivers/gpu/mediatek/gpu_mali/mali_bifrost/mali-r25p0/drivers/gpu/arm/midgard"
    if not midgard.is_dir():
        raise FileNotFoundError(f"Expected Mali Bifrost r25p0 directory missing: {midgard}")

    # 1. Fix accidental C-style comment in Kbuild
    kbuild = midgard / "Kbuild"
    kb_txt = kbuild.read_text(encoding="utf-8")
    c_comment = "/* MTK Add -I$(src)/include to include ddk linux headers: dma-buf-test-exporter.h, memory_group_manager.h, protected_mode_switcher.h */"
    if c_comment in kb_txt:
        kb_txt = kb_txt.replace(
            c_comment,
            "# MTK Add -I$(src)/include to include ddk linux headers: dma-buf-test-exporter.h, memory_group_manager.h, protected_mode_switcher.h",
        )
        kbuild.write_text(kb_txt, encoding="utf-8")
        print(f"[PATCH] Fixed C comment in {kbuild}")

    # 2. Fix typo & stale include paths in Kbuild-mtk-custom-env
    env_mk = midgard / "Kbuild-mtk-custom-env"
    env_txt = env_mk.read_text(encoding="utf-8")
    orig_env = env_txt
    env_txt = env_txt.replace("-I-I$(srctree)/include", "-I$(srctree)/include")
    env_txt = env_txt.replace(
        "-I$(srctree)/drivers/misc/mediatek/gpu/ged/include",
        "-I$(srctree)/drivers/gpu/mediatek/ged/include",
    )
    required_includes = [
        "ccflags-y += -I$(srctree)/drivers/gpu/mediatek/gpufreq",
        "ccflags-y += -I$(srctree)/drivers/gpu/mediatek/mt-plat",
        "ccflags-y += -I$(srctree)/drivers/misc/mediatek/base/power/include",
        "ccflags-y += -I$(srctree)/drivers/misc/mediatek/include/mt-plat",
    ]
    existing_lines = {ln.strip() for ln in env_txt.splitlines()}
    missing = [inc for inc in required_includes if inc not in existing_lines]
    if missing:
        anchor = "ccflags-y += -I$(srctree)/drivers/gpu/mediatek/gpufreq/$(CONFIG_MTK_PLATFORM)"
        if anchor not in env_txt:
            raise RuntimeError(f"Anchor line missing in {env_mk}")
        env_txt = env_txt.replace(anchor, anchor + "\n" + "\n".join(missing), 1)
    if env_txt != orig_env:
        env_mk.write_text(env_txt, encoding="utf-8")
        print(f"[PATCH] Updated include paths in {env_mk}")

    # 3. Fix stale gpu_bm / hal paths in platform/mt6785/Kbuild
    plat_mk = midgard / "platform/mt6785/Kbuild"
    plat_txt = plat_mk.read_text(encoding="utf-8")
    orig_plat = plat_txt
    plat_txt = plat_txt.replace(
        "-I$(srctree)/drivers/misc/mediatek/gpu/gpu_bm",
        "-I$(srctree)/drivers/gpu/mediatek/gpu_bm",
    )
    plat_txt = plat_txt.replace(
        "-I$(srctree)/drivers/misc/mediatek/gpu/hal",
        "-I$(srctree)/drivers/gpu/mediatek/hal",
    )
    if plat_txt != orig_plat:
        plat_mk.write_text(plat_txt, encoding="utf-8")
        print(f"[PATCH] Fixed platform include paths in {plat_mk}")


def _patch_414_bpfload_uname_spoof(kernel_dir: Path) -> None:
    sysc = kernel_dir / "kernel/sys.c"
    txt = sysc.read_text(encoding="utf-8")
    if "hyper_bpfloader" in txt:
        print(f"[PATCH] Targeted bpfloader uname override already present in {sysc}")
        return
    pattern = re.compile(
        r"(static int override_release\(char __user \*release, size_t len\)\s*\{\s*int ret = 0;\s*)"
    )
    injection = (
        "\n\tif (strcmp(current->comm, \"netbpfload\") == 0 ||\n"
        "\t    strcmp(current->comm, \"bpfloader\") == 0 ||\n"
        "\t    strcmp(current->comm, \"hyper_bpfloader\") == 0) {\n"
        "\t\tstatic const char kver[] = \"4.19.325-perf\";\n"
        "\t\tsize_t copy = min_t(size_t, len, sizeof(kver));\n"
        "\t\treturn copy_to_user(release, kver, copy) ? -EFAULT : 0;\n"
        "\t}\n"
    )
    new_txt, count = pattern.subn(r"\1" + injection, txt, count=1)
    if count != 1:
        raise RuntimeError(f"Could not locate override_release() in {sysc}")
    sysc.write_text(new_txt, encoding="utf-8")
    print(f"[PATCH] Applied targeted bpfloader uname override in {sysc}")


def _patch_419_mt6785_dts_swtp(kernel_dir: Path) -> None:
    """Uncomment swtp / swtp_1 / ant_check nodes in 4.19 mt6785.dts so stock dtbo_a merges cleanly."""
    dts = kernel_dir / "arch/arm64/boot/dts/mediatek/mt6785.dts"
    if not dts.is_file():
        raise FileNotFoundError(f"Expected mt6785.dts missing: {dts}")
    text = dts.read_text(encoding="utf-8")
    pattern = re.compile(
        r"/\*\s*\n(\s*//add for swtp by longcheer begin[\s\S]*?ant_check_div\s*\{[\s\S]*?\};\s*)\n\s*\*/"
    )
    m = pattern.search(text)
    if m:
        new_text = text[: m.start()] + m.group(1) + text[m.end() :]
        dts.write_text(new_text, encoding="utf-8")
        print(f"[PATCH] Uncommented swtp/swtp_1/ant_check nodes in {dts}")
    elif "swtp: swtp {" in text and "swtp_1: swtp_1 {" in text:
        print(f"[PATCH] swtp/swtp_1 nodes already active in {dts}")
    else:
        raise RuntimeError(f"Could not find swtp/swtp_1 block in {dts}")


def _patch_adsp_excep_bounds(kernel_dir: Path) -> None:
    """Fix OOB read in adsp_exception_dump() when ADSP shared coredump strings lack NUL terminator."""
    excep = kernel_dir / "drivers/misc/mediatek/adsp/mt6785/adsp_excep.c"
    if not excep.is_file():
        raise FileNotFoundError(f"Expected adsp_excep.c missing: {excep}")
    text = excep.read_text(encoding="utf-8")
    if "strnstr((const char *)coredump->assert_log" in text and "%.*s" in text:
        print(f"[PATCH] adsp_excep.c bounds checks already present in {excep}")
        return

    new_text = text.replace(
        "strstr(coredump->assert_log, ADSP_TEST_EE_PATTERN)",
        "strnstr((const char *)coredump->assert_log, ADSP_TEST_EE_PATTERN, sizeof(coredump->assert_log))",
    )

    old_block = (
        '\tn += snprintf(detail + n, ADSP_AED_STR_LEN - n, "%s %s\\n",\n'
        '\t\t      pdata->name, aed_type);\n'
        '\tif (coredump) {\n'
        '\t\tn += snprintf(detail + n, ADSP_AED_STR_LEN - n,\n'
        '\t\t\t      "adsp pc=0x%08x,exccause=0x%x,excvaddr=0x%x\\n",\n'
        '\t\t\t      coredump->pc,\n'
        '\t\t\t      coredump->exccause,\n'
        '\t\t\t      coredump->excvaddr);\n'
        '\t\tn += snprintf(detail + n, ADSP_AED_STR_LEN - n,\n'
        '\t\t\t      "CRDISPATCH_KEY:ADSP exception/%s\\n",\n'
        '\t\t\t      coredump->task_name);\n'
        '\t\tn += snprintf(detail + n, ADSP_AED_STR_LEN - n, "%s",\n'
        '\t\t\t      coredump->assert_log);\n'
        '\t}'
    )
    new_block = (
        '\tmemset(detail, 0, sizeof(detail));\n'
        '\tn += scnprintf(detail + n, sizeof(detail) - n, "%s %s\\n",\n'
        '\t\t       pdata->name, aed_type);\n'
        '\tif (coredump) {\n'
        '\t\tn += scnprintf(detail + n, sizeof(detail) - n,\n'
        '\t\t\t       "adsp pc=0x%08x,exccause=0x%x,excvaddr=0x%x\\n",\n'
        '\t\t\t       coredump->pc,\n'
        '\t\t\t       coredump->exccause,\n'
        '\t\t\t       coredump->excvaddr);\n'
        '\t\tn += scnprintf(detail + n, sizeof(detail) - n,\n'
        '\t\t\t       "CRDISPATCH_KEY:ADSP exception/%.*s\\n",\n'
        '\t\t\t       (int)sizeof(coredump->task_name),\n'
        '\t\t\t       coredump->task_name);\n'
        '\t\tn += scnprintf(detail + n, sizeof(detail) - n, "%.*s",\n'
        '\t\t\t       (int)sizeof(coredump->assert_log),\n'
        '\t\t\t       coredump->assert_log);\n'
        '\t}'
    )
    if old_block not in new_text:
        raise RuntimeError(f"Could not match snprintf block in {excep}")
    new_text = new_text.replace(old_block, new_block, 1)
    excep.write_text(new_text, encoding="utf-8")
    print(f"[PATCH] Fixed OOB string reads in {excep}")


def apply_patches(kernel_dir: Path, track: str, enable_414_spoof: bool = False) -> None:
    if track not in VALID_TRACKS:
        raise ValueError(f"Unknown track {track!r}; expected one of {VALID_TRACKS}")
    _clean_version_markers(kernel_dir)
    _patch_adsp_excep_bounds(kernel_dir)
    if track == "4.19-r25p0":
        _patch_419_mali_r25p0(kernel_dir)
        _patch_419_mt6785_dts_swtp(kernel_dir)
    elif track == "4.19-r32p1":
        _patch_419_mt6785_dts_swtp(kernel_dir)
    elif track == "4.14-r25p0" and enable_414_spoof:
        _patch_414_bpfload_uname_spoof(kernel_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply Rosemary kernel source patches")
    parser.add_argument("--kernel-dir", type=Path, required=True)
    parser.add_argument("--track", choices=VALID_TRACKS, required=True)
    parser.add_argument(
        "--enable-414-bpfload-uname-spoof",
        action="store_true",
        help="For 4.14-r25p0 track only: return 4.19.325-perf uname strictly to netbpfload/bpfloader comms",
    )
    args = parser.parse_args()
    apply_patches(args.kernel_dir, args.track, args.enable_414_bpfload_uname_spoof)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
