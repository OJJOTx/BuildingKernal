# Rosemary Kernel Cloud Build (`kernel-build/`)

This self-contained directory builds rootless, banking-safe, SELinux Enforcing Linux kernels for **Redmi Note 10S Global (`rosemary` / `secret` SKU, `MT6785`)** on GitHub Actions using zero local Linux/WSL disk space on Windows.

## Available Build Tracks

| Track | Source Branch | GPU Driver | Best Use Case |
| :--- | :--- | :--- | :--- |
| **`4.19-r25p0`** *(Default)* | `lineage-23.2` (`4.19.325`) | `mali bifrost r25p0` (`r26p0-01eac0`) | **Primary daily-driver**: 100% untouched stock `V14.0.11.0.TKLMIXM` `vendor_a`, native Android 15 `NetBpfLoad`, EROFS (`LZ4_0PADDING`/`COMPACTED_2B`), `USERFAULTFD` |
| **`4.19-r32p1`** | `lineage-23.2` (`4.19.325`) | `mali valhall r32p1` | Fallback if switching `vendor_a` Mali userspace blobs to `r32p1` |
| **`4.14-r25p0`** | `lineage-22.2` (`4.14.356`) | `mali bifrost r25p0` | Native `4.14` fallback (with optional `netbpfload`/`bpfloader`-only `uname` override toggle) |

---

## How to Build & Repack `boot.img`

### 1. Push `kernel-build/` to a Private or Public GitHub Repo
Run from PowerShell (this creates a small standalone repo inside `D:\ROMING\HyperOS-Project\kernel-build\` without committing the multi-GB ROM files in the parent workspace):

```powershell
cd D:\ROMING\HyperOS-Project\kernel-build
git init
git add .
git commit -m "Add Rosemary 4.19/4.14 EROFS kernel CI pipeline"
git branch -M main
git remote add origin https://github.com/<YOUR_USER>/<YOUR_REPO>.git
git push -u origin main
```

### 2. Trigger the GitHub Actions Workflow
1. Open your GitHub repository $\rightarrow$ **Actions** $\rightarrow$ **Build Rosemary HyperOS Kernel** $\rightarrow$ **Run workflow**.
2. Leave `track: 4.19-r25p0` and `clang_version: r487747c` selected and click **Run workflow**.
3. Wait ~10–15 minutes for the build and automated checks (`verify_kernel_artifact.py`) to complete, then download the **`rosemary-kernel-4.19-r25p0`** artifact zip (`Image.gz`, `mt6785.dtb`, `rosemary.dtbo`, `config.txt`, `BUILD_INFO.txt`, `SHA256SUMS.txt`).

### 3. Repack `boot_test.img` on Windows
Extract the downloaded artifact zip (for example into `D:\ROMING\HyperOS-Project\build\kernel-test\artifacts\`) and repack over untouched stock `build\hos1-v1\boot.img`:

```powershell
python D:\ROMING\HyperOS-Project\scripts\repack_rosemary_boot.py repack `
  --base-boot D:\ROMING\HyperOS-Project\build\hos1-v1\boot.img `
  --kernel D:\ROMING\HyperOS-Project\build\kernel-test\artifacts\Image.gz `
  --dtb D:\ROMING\HyperOS-Project\build\kernel-test\artifacts\mt6785.dtb `
  --out D:\ROMING\HyperOS-Project\build\kernel-test\boot_test.img
```

*(Note: To keep 100% stock `4.14` DTB during an initial A/B isolation test, pass `--keep-stock-dtb` instead of `--dtb ...`.)*

### 4. Stage 1 Test on Running HyperOS 1 (Before Touching HyperOS 3)
Boot the phone into fastboot and flash **only `boot_a`**:

```powershell
D:\ROMING\HyperOS-Project\build\kernel-test\flash_test_kernel_hos1.bat
```

Unlock with your lockscreen PIN/pattern, then run:

```powershell
python D:\ROMING\HyperOS-Project\scripts\post_flash_verify.py --kernel-audit
```

To revert back to stock `4.14.186` `boot.img` at any time:

```powershell
D:\ROMING\HyperOS-Project\build\kernel-test\rollback_stock_boot.bat
```
