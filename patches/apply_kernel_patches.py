#!/usr/bin/env python3
"""Apply deterministic, idempotent kernel source patches before compilation."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

VALID_TRACKS = ("4.19-r25p0", "4.19-r32p1", "4.19-r32p1-hos4", "4.14-r25p0")


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


def _patch_419_ufs_bootdevice_rename(kernel_dir: Path) -> None:
    """Restore 4.14 ufs-mtk.c 'bootdevice' platform device rename in 4.19 ufs-mediatek.c.

    Stock rosemary first_stage_ramdisk (fstab.mt6785) and untouched stock V14.0.11.0.TKLMIXM
    vendor_a (fstab.mt6785, init.mt6785.rc, ueventd.rc, nvram/ccci daemons) require
    /dev/block/platform/bootdevice/by-name/* symlinks, which Android init/ueventd only
    populates when 11270000.ufshci's sysfs platform device is renamed to 'bootdevice'
    (matching ro.boot.boot_devices=bootdevice,...).
    """
    ufs_c = kernel_dir / "drivers/scsi/ufs/ufs-mediatek.c"
    if not ufs_c.is_file():
        raise FileNotFoundError(f"Expected ufs-mediatek.c missing: {ufs_c}")
    text = ufs_c.read_text(encoding="utf-8")
    if "ufs_mtk: renamed to bootdevice" in text:
        print(f"[PATCH] bootdevice rename already present in {ufs_c}")
        return

    inc_anchor = "#include <linux/rpmb.h>\n"
    if inc_anchor not in text:
        raise RuntimeError(f"Could not find include anchor in {ufs_c}")
    text = text.replace(
        inc_anchor,
        inc_anchor + "#include <linux/slab.h>\n#include <linux/string.h>\n",
        1,
    )

    probe_old = (
        "\t/* Add get_boot_type check and return ENODEV if not ufs boot */\n"
        "\tboot_type = ufs_mtk_get_boot_type();\n"
        "\tif (boot_type != BOOTDEV_UFS)\n"
        "\t\treturn -ENODEV;\n"
        "\n"
        "\t/* perform generic probe */"
    )
    probe_new = (
        "\t/* Add get_boot_type check and return ENODEV if not ufs boot */\n"
        "\tboot_type = ufs_mtk_get_boot_type();\n"
        "\tif (boot_type != BOOTDEV_UFS)\n"
        "\t\treturn -ENODEV;\n"
        "\n"
        "\t/* Match 4.14 ufs-mtk.c & mtk-sd.c UAF-safe rename so ueventd populates\n"
        "\t * /dev/block/platform/bootdevice/by-name/* for first_stage_mount & vendor_a.\n"
        "\t */\n"
        "\tif (strcmp(dev_name(dev), \"bootdevice\") != 0) {\n"
        "\t\tconst char *dup_name = kstrdup(pdev->name, GFP_KERNEL);\n"
        "\n"
        "\t\tpdev->name = dup_name;\n"
        "\t\tif (!device_rename(dev, \"bootdevice\"))\n"
        "\t\t\tdev_info(dev, \"ufs_mtk: renamed to bootdevice\\n\");\n"
        "\t\tpdev->name = pdev->dev.kobj.name;\n"
        "\t\tkfree_const(dup_name);\n"
        "\t}\n"
        "\n"
        "\t/* perform generic probe */"
    )
    if probe_old not in text:
        raise RuntimeError(f"Could not match ufs_mtk_probe block in {ufs_c}")
    text = text.replace(probe_old, probe_new, 1)
    ufs_c.write_text(text, encoding="utf-8")
    print(f"[PATCH] Added UAF-safe 'bootdevice' rename to ufs_mtk_probe in {ufs_c}")


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

    # 4. Restore Bifrost DT compatible on mali@13040000 so mali-r25p0 kbase_dt_ids[] probes /dev/mali0
    dts = kernel_dir / "arch/arm64/boot/dts/mediatek/mt6785.dts"
    dts_txt = dts.read_text(encoding="utf-8")
    valhall_compat = '\tmali@13040000 {\n\t\tcompatible =\n\t\t\t"mediatek,mali", "arm,mali-valhall";'
    bifrost_compat = (
        '\tmali@13040000 {\n\t\tcompatible =\n\t\t\t"mediatek,mali", "arm,mali-midgard", "arm,mali-bifrost";'
    )
    if valhall_compat in dts_txt:
        dts_txt = dts_txt.replace(valhall_compat, bifrost_compat, 1)
        dts.write_text(dts_txt, encoding="utf-8")
        print(f"[PATCH] Restored 'arm,mali-midgard', 'arm,mali-bifrost' compatible on mali@13040000 in {dts}")
    elif bifrost_compat in dts_txt:
        print(f"[PATCH] mali@13040000 Bifrost compatible already present in {dts}")
    else:
        raise RuntimeError(f"Could not match mali@13040000 compatible block in {dts}")


def _patch_419_mt6785_dts_rosemary_compat(kernel_dir: Path) -> None:
    """Ensure 4.19 mt6785.dts merges cleanly with untouched stock 4.14 dtbo_a and retains 4.19 board props."""
    dts = kernel_dir / "arch/arm64/boot/dts/mediatek/mt6785.dts"
    if not dts.is_file():
        raise FileNotFoundError(f"Expected mt6785.dts missing: {dts}")
    text = dts.read_text(encoding="utf-8")
    orig = text

    # 1. Uncomment swtp / swtp_1 / ant_check / ant_check_div symbols required by stock dtbo_a __fixups__
    pattern = re.compile(
        r"/\*\s*\n(\s*//add for swtp by longcheer begin[\s\S]*?ant_check_div\s*\{[\s\S]*?\};\s*)\n\s*\*/"
    )
    m = pattern.search(text)
    if m:
        text = text[: m.start()] + m.group(1) + text[m.end() :]
        print(f"[PATCH] Uncommented swtp/swtp_1/ant_check nodes in {dts}")
    elif "swtp: swtp {" not in text or "swtp_1: swtp_1 {" not in text:
        raise RuntimeError(f"Could not find swtp/swtp_1 block in {dts}")

    # 2. Apply 4.19 rosemary.dts PE+/PD charger properties directly to base lk_charger & charger nodes
    lk_old = "\tlk_charger: lk_charger {\n\t\tcompatible = \"mediatek,lk_charger\";\n\t\tenable_anime;\n\t\t/* enable_pe_plus; */"
    lk_new = "\tlk_charger: lk_charger {\n\t\tcompatible = \"mediatek,lk_charger\";\n\t\tenable_anime;\n\t\tenable_pe_plus;"
    if lk_old in text:
        text = text.replace(lk_old, lk_new, 1)

    chg_old = (
        "\t\tenable_sw_jeita;\n"
        "\t\t/* enable_pe_plus; */\n"
        "\t\t/* enable_pe_2; */\n"
        "\t\t/* enable_pe_3; */\n"
        "\t\t/* enable_pe_4; */"
    )
    chg_new = (
        "\t\tenable_sw_jeita;\n"
        "\t\tenable_pe_plus;\n"
        "\t\tenable_pe_2;\n"
        "\t\t/* enable_pe_3; */\n"
        "\t\tenable_pe_4;"
    )
    if chg_old in text:
        text = text.replace(chg_old, chg_new, 1)
    if "\t\tpd_vbus_upper_bound = <9000000>;" in text:
        text = text.replace("\t\tpd_vbus_upper_bound = <9000000>;", "\t\tpd_vbus_upper_bound = <12000000>;", 1)

    # 3. Add 4.19 rosemary.dts keypad GPIO80 pinctrl default to mt6785.dts
    haptic_end = "}; /* HAPTIC GPIO Settings -End */"
    kpd_snippet = (
        "\n\n&keypad {\n"
        "\tpinctrl-names = \"default\";\n"
        "\tpinctrl-0 = <&kpd_pins_default>;\n"
        "};\n\n"
        "&pio {\n"
        "\tkpd_pins_default: kpdgpiodefault {\n"
        "\t\tpins_cmd_dat {\n"
        "\t\t\tpinmux = <PINMUX_GPIO80__FUNC_BPI_BUS8>;\n"
        "\t\t\tbias-pull-up = <MTK_PUPD_SET_R1R0_10>;\n"
        "\t\t\tinput-enable;\n"
        "\t\t\tinput-schmitt-enable;\n"
        "\t\t};\n"
        "\t};\n"
        "};"
    )
    if "kpd_pins_default: kpdgpiodefault" not in text:
        if haptic_end not in text:
            raise RuntimeError(f"Could not find HAPTIC GPIO anchor in {dts}")
        text = text.replace(haptic_end, haptic_end + kpd_snippet, 1)

    # 4. Size reusable ssmr_cma_mem to 0x14000000 (320 MiB = 256 MiB SVP + 64 MiB WFD) matching stock 4.14 svp_scheme
    ssmr_old = (
        "\t\tssmr_cma_mem: ssmr-reserved-cma_memory {\n"
        "\t\t\tcompatible = \"shared-dma-pool\";\n"
        "\t\t\treusable;\n"
        "\t\t\tsize = <0 0x10000000>;"
    )
    ssmr_new = (
        "\t\tssmr_cma_mem: ssmr-reserved-cma_memory {\n"
        "\t\t\tcompatible = \"shared-dma-pool\";\n"
        "\t\t\treusable;\n"
        "\t\t\tsize = <0 0x14000000>;"
    )
    if ssmr_old in text:
        text = text.replace(ssmr_old, ssmr_new, 1)

    if text != orig:
        dts.write_text(text, encoding="utf-8")
        print(f"[PATCH] Updated rosemary charger, keypad & SSMR CMA DT properties in {dts}")

    # 5. Match 4.19 rosemary.dts mt6360_pmic pwr_off_seq
    mt6360_dtsi = kernel_dir / "arch/arm64/boot/dts/mediatek/v1/mt6360.dtsi"
    if mt6360_dtsi.is_file():
        m_txt = mt6360_dtsi.read_text(encoding="utf-8")
        if "pwr_off_seq = [06 04 00 02];" in m_txt:
            mt6360_dtsi.write_text(
                m_txt.replace("pwr_off_seq = [06 04 00 02];", "pwr_off_seq = [04 00 00 02];", 1),
                encoding="utf-8",
            )
            print(f"[PATCH] Updated mt6360_pmic pwr_off_seq in {mt6360_dtsi}")


def _patch_419_cameraisp_owe(kernel_dir: Path) -> None:
    """Wire drivers/misc/mediatek/cameraisp/owe (checked out from lineage-24.0) into Kbuild/Kconfig."""
    owe_dir = kernel_dir / "drivers/misc/mediatek/cameraisp/owe"
    if not (owe_dir / "mt6785/camera_owe.c").is_file():
        raise FileNotFoundError(
            f"Missing {owe_dir / 'mt6785/camera_owe.c'} (ensure lineage-24.0 cameraisp/owe was checked out)"
        )

    kcfg = kernel_dir / "drivers/misc/mediatek/cameraisp/Kconfig"
    kcfg_txt = kcfg.read_text(encoding="utf-8")
    if '#source "drivers/misc/mediatek/cameraisp/owe/Kconfig"' in kcfg_txt:
        kcfg.write_text(
            kcfg_txt.replace(
                '#source "drivers/misc/mediatek/cameraisp/owe/Kconfig"',
                'source "drivers/misc/mediatek/cameraisp/owe/Kconfig"',
                1,
            ),
            encoding="utf-8",
        )
        print(f"[PATCH] Enabled cameraisp/owe/Kconfig in {kcfg}")

    mk = kernel_dir / "drivers/misc/mediatek/cameraisp/Makefile"
    mk_txt = mk.read_text(encoding="utf-8")
    if (
        "obj-$(CONFIG_MTK_CAMERA_ISP_OWE_SUPPORT) += owe/" not in mk_txt
        and "ifeq ($(CONFIG_MACH_MT6785),y)\nobj-y += owe/" not in mk_txt
    ):
        old_owe = "#ifeq ($(CONFIG_MACH_MT6763),y)\n#obj-y += owe/\n#endif"
        if old_owe not in mk_txt:
            raise RuntimeError(f"Could not find commented owe block in {mk}")
        mk.write_text(
            mk_txt.replace(old_owe, "obj-$(CONFIG_MTK_CAMERA_ISP_OWE_SUPPORT) += owe/", 1),
            encoding="utf-8",
        )
        print(f"[PATCH] Enabled cameraisp/owe build rule in {mk}")

    owe_mk = owe_dir / "Makefile"
    owe_mk_txt = owe_mk.read_text(encoding="utf-8")
    if "-I$(srctree)/drivers/iommu" not in owe_mk_txt:
        owe_mk.write_text(
            owe_mk_txt.replace(
                "ccflags-y += -I$(srctree)/drivers/misc/mediatek/smi/",
                "ccflags-y += -I$(srctree)/drivers/misc/mediatek/smi/\nccflags-y += -I$(srctree)/drivers/iommu",
                1,
            ),
            encoding="utf-8",
        )
        print(f"[PATCH] Added iommu include path to {owe_mk}")


def _patch_419_display_charger_wifi_fixes(kernel_dir: Path) -> None:
    """Apply upstream lineage-24.0 & community stability fixes for display, PD charging, thermal, and Wi-Fi."""
    # 1. mtkfb.c: Fix console_lock deadlock in mtkfb_blank (e67c7e122) & stale FP unblank state (c6e68b3e6)
    mtkfb = kernel_dir / "drivers/misc/mediatek/video/mt6785/videox/mtkfb.c"
    fb_txt = mtkfb.read_text(encoding="utf-8")
    orig_fb = fb_txt
    if "cancel_delayed_work_sync(&prim_panel_work);" in fb_txt:
        fb_txt = fb_txt.replace(
            "cancel_delayed_work_sync(&prim_panel_work);",
            "cancel_delayed_work(&prim_panel_work);",
            1,
        )
    pd_old = (
        "\tcase FB_BLANK_POWERDOWN:\n"
        "\t\tDISPDBG(\"%s mtkfb_early_suspend\\n\", __func__);\n"
        "\t\tif (bypass_blank) {"
    )
    pd_new = (
        "\tcase FB_BLANK_POWERDOWN:\n"
        "\t\tDISPDBG(\"%s mtkfb_early_suspend\\n\", __func__);\n"
        "\t\tif (info == prim_fbi) {\n"
        "\t\t\tatomic_set(&prim_panel_is_on, false);\n"
        "\t\t\tcancel_delayed_work(&prim_panel_work);\n"
        "\t\t}\n"
        "\t\tif (bypass_blank) {"
    )
    if pd_old in fb_txt and "if (info == prim_fbi)" not in fb_txt[fb_txt.find("case FB_BLANK_POWERDOWN:") :]:
        fb_txt = fb_txt.replace(pd_old, pd_new, 1)
    if fb_txt != orig_fb:
        mtkfb.write_text(fb_txt, encoding="utf-8")
        print(f"[PATCH] Applied mtkfb_blank deadlock and FP unblank fixes in {mtkfb}")

    # 2. mtk_ts_btspdchg.c: Fix double ADC scaling (70dc56eca) so 33W PD charger NTC reads true millivolts
    bts = kernel_dir / "drivers/misc/mediatek/thermal/common/thermal_zones/mtk_ts_btspdchg.c"
    bts_txt = bts.read_text(encoding="utf-8")
    orig_bts = bts_txt
    bts_txt = re.sub(
        r'\tpr_err\("%s\s+%d thermistor_ch5 not NULL \.\.\.\.\.\. \\n", __func__, __LINE__\);\n',
        "",
        bts_txt,
        count=1,
    )
    if "\t/*val * 1500 / 4096*/\n\tret = (val * 1500) >> 12;" in bts_txt:
        bts_txt = bts_txt.replace(
            "\t/*val * 1500 / 4096*/\n\tret = (val * 1500) >> 12;",
            "\t/* mt6577_auxadc already returns millivolts for processed reads */\n\tret = val;",
            1,
        )
    if bts_txt != orig_bts:
        bts.write_text(bts_txt, encoding="utf-8")
        print(f"[PATCH] Fixed PD charger NTC double ADC scaling in {bts}")

    # 3. mtk_charger.c: Clamp negative system_temp_level OOB index (8436741fa)
    chg = kernel_dir / "drivers/power/supply/mediatek/charger/mtk_charger.c"
    chg_txt = chg.read_text(encoding="utf-8")
    chg_old = (
        "    power_supply_get_property(pinfo->usb_psy,\n"
        "\t\t\tPOWER_SUPPLY_PROP_REAL_TYPE, &real_type);\n\n"
        "\tif (temp_level > pinfo->system_temp_level_max - 1)"
    )
    chg_new = (
        "    power_supply_get_property(pinfo->usb_psy,\n"
        "\t\t\tPOWER_SUPPLY_PROP_REAL_TYPE, &real_type);\n\n"
        "\tif (temp_level < 0)\n"
        "\t\tpinfo->system_temp_level = 0;\n"
        "\telse if (temp_level > pinfo->system_temp_level_max - 1)"
    )
    if chg_old in chg_txt:
        chg.write_text(chg_txt.replace(chg_old, chg_new, 1), encoding="utf-8")
        print(f"[PATCH] Clamped negative thermal temp_level in {chg}")

    # 4. mtk_pd_adapter.c: Clear PD_VERIFY_IN_PROCESS on disconnect/hard-reset (8fb47d6a0) so USB ADB recovers
    pda = kernel_dir / "drivers/power/supply/mediatek/charger/mtk_pd_adapter.c"
    pda_txt = pda.read_text(encoding="utf-8")
    if "union power_supply_propval pd_verify = {0,};" not in pda_txt:
        pda_txt = pda_txt.replace(
            "\tunion power_supply_propval pd_authentication = {0,};\n",
            "\tunion power_supply_propval pd_authentication = {0,};\n\tunion power_supply_propval pd_verify = {0,};\n",
            1,
        )
        pda_txt = pda_txt.replace(
            "\t\t\tpinfo->adapter_dev->verify_process = 0;\n\t\t\tnotify_adapter_event(MTK_PD_ADAPTER,\n\t\t\t\tMTK_PD_CONNECT_NONE, NULL);",
            "\t\t\tpinfo->adapter_dev->verify_process = 0;\n\t\t\tpower_supply_set_property(usb_psy, POWER_SUPPLY_PROP_PD_VERIFY_IN_PROCESS, &pd_verify);\n\t\t\tnotify_adapter_event(MTK_PD_ADAPTER,\n\t\t\t\tMTK_PD_CONNECT_NONE, NULL);",
            1,
        )
        pda_txt = pda_txt.replace(
            "\t\t\tret = power_supply_set_property(usb_psy, POWER_SUPPLY_PROP_PD_AUTHENTICATION, &pd_authentication);\n",
            "\t\t\tret = power_supply_set_property(usb_psy, POWER_SUPPLY_PROP_PD_AUTHENTICATION, &pd_authentication);\n\t\t\tpower_supply_set_property(usb_psy, POWER_SUPPLY_PROP_PD_VERIFY_IN_PROCESS, &pd_verify);\n",
            1,
        )
        pda.write_text(pda_txt, encoding="utf-8")
        print(f"[PATCH] Cleared PD verify flag on disconnect in {pda}")

    # 5. gen4m axi.c: Guard of_reserved_mem_device_init against pre-assigned dma_mem (56e06b2243)
    axi = kernel_dir / "drivers/misc/mediatek/connectivity/wlan/core/gen4m/os/linux/hif/axi/axi.c"
    axi_txt = axi.read_text(encoding="utf-8")
    axi_old = (
        "\tret = _init_resv_mem(pdev);\n"
        "\tif (ret)\n"
        "\t\tgoto exit;\n"
        "\tret = of_reserved_mem_device_init(&pdev->dev);\n"
        "\tif (ret) {\n"
        "\t\tDBGLOG(INIT, ERROR, \"of_reserved_mem_device_init failed(%d).\\n\",\n"
        "\t\t\t\tret);\n"
        "\t\tgoto exit;\n"
        "\t}"
    )
    axi_new = (
        "\tret = _init_resv_mem(pdev);\n"
        "\tif (ret)\n"
        "\t\tgoto exit;\n"
        "\tif (!pdev->dev.dma_mem) {\n"
        "\t\tret = of_reserved_mem_device_init(&pdev->dev);\n"
        "\t\tif (ret) {\n"
        "\t\t\tDBGLOG(INIT, ERROR, \"of_reserved_mem_device_init failed(%d).\\n\",\n"
        "\t\t\t\t\tret);\n"
        "\t\t\tgoto exit;\n"
        "\t\t}\n"
        "\t}"
    )
    if "if (!pdev->dev.dma_mem)" not in axi_txt and "if (ret == -EBUSY)" not in axi_txt:
        if axi_old not in axi_txt:
            raise RuntimeError(f"Could not match axiDmaSetup block in {axi}")
        axi.write_text(axi_txt.replace(axi_old, axi_new, 1), encoding="utf-8")
        print(f"[PATCH] Guarded DMA coherent pool init in {axi}")

    # 6. TEEI 400 lineage-24.0 / 22.2 parity: drop DEBUG_DO/-Werror & restore CONFIG_MICROTRUST_TZ_LOG guards
    tzd_mk = kernel_dir / "drivers/tee/teei/400/tz_driver/Makefile"
    if tzd_mk.is_file():
        tzd_txt = tzd_mk.read_text(encoding="utf-8")
        if "ccflags-y += -D DEBUG_DO\n" in tzd_txt:
            tzd_mk.write_text(tzd_txt.replace("ccflags-y += -D DEBUG_DO\n", "", 1), encoding="utf-8")
            print(f"[PATCH] Dropped -D DEBUG_DO in {tzd_mk}")

    dcih_mk = kernel_dir / "drivers/tee/teei/400/tz_dcih/Makefile"
    if dcih_mk.is_file():
        dcih_txt = dcih_mk.read_text(encoding="utf-8")
        if "ccflags-y += -D DEBUG_DO -fno-pic -Werror" in dcih_txt:
            dcih_mk.write_text(
                dcih_txt.replace(
                    "ccflags-y += -D DEBUG_DO -fno-pic -Werror",
                    "ccflags-y += -D DEBUG_DO -fno-pic",
                    1,
                ),
                encoding="utf-8",
            )
            print(f"[PATCH] Dropped -Werror in {dcih_mk}")

    sq_c = kernel_dir / "drivers/tee/teei/400/tz_driver/switch_queue.c"
    if sq_c.is_file():
        sq_txt = sq_c.read_text(encoding="utf-8")
        sq_old = "\t\tif (retVal == 1)\n\t\t\tcontinue;\n\n\t\tteei_notify_log_fn();\n"
        sq_new = (
            "\t\tif (retVal == 1)\n\t\t\tcontinue;\n\n"
            "#ifdef CONFIG_MICROTRUST_TZ_LOG\n\t\tteei_notify_log_fn();\n#endif\n"
        )
        if "#ifdef CONFIG_MICROTRUST_TZ_LOG" not in sq_txt and sq_old in sq_txt:
            sq_c.write_text(sq_txt.replace(sq_old, sq_new, 1), encoding="utf-8")
            print(f"[PATCH] Restored CONFIG_MICROTRUST_TZ_LOG guard in {sq_c}")

    tcm_c = kernel_dir / "drivers/tee/teei/400/tz_driver/teei_client_main.c"
    if tcm_c.is_file():
        tcm_txt = tcm_c.read_text(encoding="utf-8")
        tcm_old = (
            "\twake_up_process(teei_bdrv_task);\n\n"
            "\tinit_tlog_comp_fn();\n\n"
            "\t/* create the teei log thread */\n"
            "\tteei_log_task = kthread_create(teei_log_fn, NULL, \"teei_log_thread\");\n"
            "\tif (IS_ERR(teei_log_task)) {\n"
            "\t\tIMSG_ERROR(\"create teei log thread failed: %ld\\n\",\n"
            "\t\t\t\t\t\tPTR_ERR(teei_log_task));\n"
            "\t\tteei_log_task = NULL;\n"
            "\t\tgoto class_device_destroy;\n"
            "\t}\n\n"
            "\twake_up_process(teei_log_task);\n"
        )
        tcm_new = (
            "\twake_up_process(teei_bdrv_task);\n\n"
            "#ifdef CONFIG_MICROTRUST_TZ_LOG\n"
            "\tinit_tlog_comp_fn();\n\n"
            "\t/* create the teei log thread */\n"
            "\tteei_log_task = kthread_create(teei_log_fn, NULL, \"teei_log_thread\");\n"
            "\tif (IS_ERR(teei_log_task)) {\n"
            "\t\tIMSG_ERROR(\"create teei log thread failed: %ld\\n\",\n"
            "\t\t\t\t\t\tPTR_ERR(teei_log_task));\n"
            "\t\tteei_log_task = NULL;\n"
            "\t\tgoto class_device_destroy;\n"
            "\t}\n\n"
            "\twake_up_process(teei_log_task);\n"
            "#endif\n"
        )
        if "#ifdef CONFIG_MICROTRUST_TZ_LOG\n\tinit_tlog_comp_fn();" not in tcm_txt and tcm_old in tcm_txt:
            tcm_c.write_text(tcm_txt.replace(tcm_old, tcm_new, 1), encoding="utf-8")
            print(f"[PATCH] Restored CONFIG_MICROTRUST_TZ_LOG guard in {tcm_c}")

    # 7. mtk_gpufreq_core.c: Prevent ERR_PTR dereference in __mt_gpufreq_init_efuse on nvmem error
    gpufreq_c = kernel_dir / "drivers/gpu/mediatek/gpufreq/mt6785/mtk_gpufreq_core.c"
    gpu_txt = gpufreq_c.read_text(encoding="utf-8")
    gpu_old = (
        '    efuse_cell = nvmem_cell_get(&pdev->dev, "efuse_segment_cell");\n'
        '    if (IS_ERR(efuse_cell)) {\n'
        '      PTR_ERR(efuse_cell);\n'
        '    }\n\n'
        '    efuse_buf = (unsigned int *)nvmem_cell_read(efuse_cell, &efuse_len);\n'
        '    nvmem_cell_put(efuse_cell);\n'
        '    if (IS_ERR(efuse_buf)) {\n'
        '      PTR_ERR(efuse_buf);\n'
        '    }\n\n'
        '    g_efuse_id = (*efuse_buf & 0xFF);\n'
        '    kfree(efuse_buf);'
    )
    gpu_new = (
        '    efuse_cell = nvmem_cell_get(&pdev->dev, "efuse_segment_cell");\n'
        '    if (IS_ERR(efuse_cell)) {\n'
        '        g_efuse_id = 0x0;\n'
        '    } else {\n'
        '        efuse_buf = (unsigned int *)nvmem_cell_read(efuse_cell, &efuse_len);\n'
        '        nvmem_cell_put(efuse_cell);\n'
        '        if (IS_ERR_OR_NULL(efuse_buf)) {\n'
        '            g_efuse_id = 0x0;\n'
        '        } else {\n'
        '            g_efuse_id = (*efuse_buf & 0xFF);\n'
        '            kfree(efuse_buf);\n'
        '        }\n'
        '    }'
    )
    if "IS_ERR_OR_NULL(efuse_buf)" not in gpu_txt:
        if gpu_old not in gpu_txt:
            raise RuntimeError(f"Could not match __mt_gpufreq_init_efuse block in {gpufreq_c}")
        gpufreq_c.write_text(gpu_txt.replace(gpu_old, gpu_new, 1), encoding="utf-8")
        print(f"[PATCH] Guarded ERR_PTR dereference in {gpufreq_c}")


IORAP_DEV_C = """\
// SPDX-License-Identifier: GPL-2.0-only
/*
 * Minimal /dev/iorap_dev stub for HyperOS launch-boost iorapd.
 * Prevents iorapd inside com.android.profiling APEX from exiting fatally
 * and triggering apexd rollback reboots when proprietary iorap_dev is absent.
 */

#include <linux/cdev.h>
#include <linux/device.h>
#include <linux/fs.h>
#include <linux/module.h>

#define IORAP_DEV_NAME "iorap_dev"

static dev_t iorap_devno;
static struct cdev iorap_cdev;
static struct class *iorap_class;

static int iorap_open(struct inode *inode, struct file *filp)
{
\treturn 0;
}

static long iorap_ioctl(struct file *filp, unsigned int cmd, unsigned long arg)
{
\treturn -ENOTTY;
}

static char *iorap_devnode(struct device *dev, umode_t *mode)
{
\t*mode = 0666;
\treturn NULL;
}

static const struct file_operations iorap_fops = {
\t.owner          = THIS_MODULE,
\t.open           = iorap_open,
\t.unlocked_ioctl = iorap_ioctl,
\t.compat_ioctl   = iorap_ioctl,
};

static int __init iorap_init(void)
{
\tstruct device *dev;
\tint err;

\terr = alloc_chrdev_region(&iorap_devno, 0, 1, IORAP_DEV_NAME);
\tif (err < 0)
\t\treturn err;

\tcdev_init(&iorap_cdev, &iorap_fops);
\tiorap_cdev.owner = THIS_MODULE;
\terr = cdev_add(&iorap_cdev, iorap_devno, 1);
\tif (err < 0)
\t\tgoto err_cdev;

\tiorap_class = class_create(THIS_MODULE, IORAP_DEV_NAME);
\tif (IS_ERR(iorap_class)) {
\t\terr = PTR_ERR(iorap_class);
\t\tgoto err_class;
\t}
\tiorap_class->devnode = iorap_devnode;

\tdev = device_create(iorap_class, NULL, iorap_devno, NULL, IORAP_DEV_NAME);
\tif (IS_ERR(dev)) {
\t\terr = PTR_ERR(dev);
\t\tgoto err_device;
\t}

\tpr_info("%s: stub device registered (maj %d)\\n", IORAP_DEV_NAME,
\t\tMAJOR(iorap_devno));
\treturn 0;

err_device:
\tclass_destroy(iorap_class);
err_class:
\tcdev_del(&iorap_cdev);
err_cdev:
\tunregister_chrdev_region(iorap_devno, 1);
\treturn err;
}

static void __exit iorap_exit(void)
{
\tdevice_destroy(iorap_class, iorap_devno);
\tclass_destroy(iorap_class);
\tcdev_del(&iorap_cdev);
\tunregister_chrdev_region(iorap_devno, 1);
}

module_init(iorap_init);
module_exit(iorap_exit);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Stub /dev/iorap_dev for HyperOS launch-boost iorapd");
"""


def _patch_419_iorap_dev_stub(kernel_dir: Path) -> None:
    """Add minimal /dev/iorap_dev stub so HyperOS 3 iorapd does not trigger apexd rollback reboots."""
    misc_dir = kernel_dir / "drivers/misc"
    iorap_c = misc_dir / "iorap_dev.c"
    if not iorap_c.exists() or iorap_c.read_text(encoding="utf-8") != IORAP_DEV_C:
        iorap_c.write_text(IORAP_DEV_C, encoding="utf-8")
        print(f"[PATCH] Wrote {iorap_c}")

    misc_mk = misc_dir / "Makefile"
    mk_txt = misc_mk.read_text(encoding="utf-8")
    if "obj-y += iorap_dev.o" not in mk_txt:
        anchor = "obj-y += board_id.o\n"
        if anchor in mk_txt:
            misc_mk.write_text(mk_txt.replace(anchor, anchor + "obj-y += iorap_dev.o\n", 1), encoding="utf-8")
        else:
            misc_mk.write_text(mk_txt.rstrip() + "\nobj-y += iorap_dev.o\n", encoding="utf-8")
        print(f"[PATCH] Added iorap_dev.o to {misc_mk}")


def _patch_419_kallsyms_namebuf_compat(kernel_dir: Path) -> None:
    """Restore KSYM_NAME_LEN=128 when CFI is disabled so out-of-tree MediaTek callers
    passing 'char namebuf[128]' to kallsyms_lookup() (e.g. spm_v4/mtk_idle.c) do not
    suffer a 64-byte stack buffer overflow from namebuf[KSYM_NAME_LEN - 1] = 0."""
    kh = kernel_dir / "include/linux/kallsyms.h"
    if kh.is_file():
        ktxt = kh.read_text(encoding="utf-8")
        if "#define KSYM_NAME_LEN 192" in ktxt:
            kh.write_text(ktxt.replace("#define KSYM_NAME_LEN 192", "#define KSYM_NAME_LEN 128", 1), encoding="utf-8")
            print(f"[PATCH] Restored KSYM_NAME_LEN=128 in {kh} to prevent MediaTek stack overflow")

    mtk_idle = kernel_dir / "drivers/misc/mediatek/base/power/spm_v4/mtk_idle.c"
    if mtk_idle.is_file():
        itxt = mtk_idle.read_text(encoding="utf-8")
        if "char namebuf[128] = {0};" in itxt:
            mtk_idle.write_text(
                itxt.replace("char namebuf[128] = {0};", "char namebuf[KSYM_NAME_LEN] = {0};", 1),
                encoding="utf-8",
            )
            print(f"[PATCH] Fixed stack buffer size char namebuf[KSYM_NAME_LEN] in {mtk_idle}")


def _patch_bpfload_uname_spoof(kernel_dir: Path, kver_str: str = "4.19.325-perf") -> None:
    sysc = kernel_dir / "kernel/sys.c"
    txt = sysc.read_text(encoding="utf-8")
    if "should_spoof_bpf_uname" in txt:
        print(f"[PATCH] Targeted bpfloader/connectivity uname override already present in {sysc}")
        return

    inc_anchor = "#include <generated/utsrelease.h>\n"
    if inc_anchor not in txt:
        raise RuntimeError(f"Could not locate utsrelease.h include in {sysc}")
    txt = txt.replace(
        inc_anchor,
        inc_anchor + "#include <linux/dcache.h>\n#include <linux/sched/task_stack.h>\n",
        1,
    )

    helper_fn = (
        "static bool should_spoof_bpf_uname(void)\n"
        "{\n"
        "\tconst char *comm = current->comm;\n"
        "\tconst char *gl_comm = current->group_leader ? current->group_leader->comm : comm;\n"
        "#ifdef CONFIG_ARM64\n"
        "\tstruct mm_struct *mm;\n"
        "#endif\n"
        "\n"
        "\tif (strcmp(comm, \"netbpfload\") == 0 ||\n"
        "\t    strcmp(comm, \"NetBpfLoad\") == 0 ||\n"
        "\t    strcmp(comm, \"bpfloader\") == 0 ||\n"
        "\t    strcmp(comm, \"uprobestatsbpfl\") == 0 ||\n"
        "\t    strcmp(comm, \"hyper_bpfloader\") == 0 ||\n"
        "\t    strcmp(comm, \"miuibpfservice\") == 0 ||\n"
        "\t    strcmp(comm, \"conn_info\") == 0 ||\n"
        "\t    strcmp(comm, \"conn_info_d\") == 0 ||\n"
        "\t    strcmp(comm, \"netd\") == 0 ||\n"
        "\t    strcmp(gl_comm, \"netd\") == 0 ||\n"
        "\t    strcmp(comm, \"clatd\") == 0 ||\n"
        "\t    strcmp(gl_comm, \"clatd\") == 0 ||\n"
        "\t    __kuid_val(current_uid()) == 1073)\n"
        "\t\treturn true;\n"
        "\n"
        "#ifdef CONFIG_ARM64\n"
        "\t/*\n"
        "\t * Android 17 Connectivity APEX native libraries (libservice-connectivity.so,\n"
        "\t * libnetworkstats.so, libandroid_net_connectivity_com_android_net_module_util_jni.so,\n"
        "\t * libnetd_updatable.so, libnetd_resolv.so) inline BpfUtils.h getLinuxKernelVersion()\n"
        "\t * and call uname() directly via PLT into Bionic libc.so, leaving LR inside the\n"
        "\t * calling Connectivity .so VMA while keeping true 4.19.325-perf for VINTF/Settings.\n"
        "\t */\n"
        "\tmm = current->mm;\n"
        "\tif (mm && !(current->flags & PF_KTHREAD)) {\n"
        "\t\tstruct pt_regs *regs = task_pt_regs(current);\n"
        "\t\tunsigned long lr = compat_user_mode(regs) ?\n"
        "\t\t\t\t   regs->compat_lr : regs->regs[30];\n"
        "\t\tstruct vm_area_struct *vma;\n"
        "\n"
        "\t\tdown_read(&mm->mmap_sem);\n"
        "\t\tvma = find_vma(mm, lr);\n"
        "\t\tif (vma && vma->vm_start <= lr && vma->vm_file &&\n"
        "\t\t    vma->vm_file->f_path.dentry) {\n"
        "\t\t\tconst char *dname = vma->vm_file->f_path.dentry->d_name.name;\n"
        "\n"
        "\t\t\tif (strstr(dname, \"connectivity\") ||\n"
        "\t\t\t    strstr(dname, \"networkstats\") ||\n"
        "\t\t\t    strstr(dname, \"netd\") ||\n"
        "\t\t\t    strstr(dname, \"bpf\") ||\n"
        "\t\t\t    strstr(dname, \"tethering\")) {\n"
        "\t\t\t\tup_read(&mm->mmap_sem);\n"
        "\t\t\t\treturn true;\n"
        "\t\t\t}\n"
        "\t\t}\n"
        "\t\tup_read(&mm->mmap_sem);\n"
        "\t}\n"
        "#endif\n"
        "\treturn false;\n"
        "}\n\n"
    )

    pattern = re.compile(
        r"(static int override_release\(char __user \*release, size_t len\)\s*\{\s*int ret = 0;\s*)"
    )
    injection = (
        "\n\tif (should_spoof_bpf_uname()) {\n"
        f"\t\tstatic const char kver[] = \"{kver_str}\";\n"
        "\t\tsize_t copy = min_t(size_t, len, sizeof(kver));\n"
        "\t\treturn copy_to_user(release, kver, copy) ? -EFAULT : 0;\n"
        "\t}\n"
    )
    new_txt, count = pattern.subn(helper_fn + r"\1" + injection, txt, count=1)
    if count != 1:
        raise RuntimeError(f"Could not locate override_release() in {sysc}")
    sysc.write_text(new_txt, encoding="utf-8")
    print(f"[PATCH] Applied targeted bpfloader/connectivity uname override ({kver_str}) in {sysc}")


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
    if track in ("4.19-r25p0", "4.19-r32p1", "4.19-r32p1-hos4"):
        _patch_419_ufs_bootdevice_rename(kernel_dir)
        _patch_419_mt6785_dts_rosemary_compat(kernel_dir)
        _patch_419_cameraisp_owe(kernel_dir)
        _patch_419_display_charger_wifi_fixes(kernel_dir)
        _patch_419_iorap_dev_stub(kernel_dir)
        _patch_419_kallsyms_namebuf_compat(kernel_dir)
        if track == "4.19-r25p0":
            _patch_419_mali_r25p0(kernel_dir)
        elif track == "4.19-r32p1-hos4":
            _patch_bpfload_uname_spoof(kernel_dir, kver_str="5.10.239-perf")
    elif track == "4.14-r25p0" and enable_414_spoof:
        _patch_bpfload_uname_spoof(kernel_dir, kver_str="4.19.325-perf")


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
