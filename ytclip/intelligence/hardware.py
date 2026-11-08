"""What this PC can run a local language model on: memory, CPU threads and the graphics card.

Nothing here starts a program that a virus scanner might flag (no PowerShell, no WMI): Windows is read
through the registry and kernel32, Linux through /sys and /proc, macOS through sysctl. NVIDIA cards
are also asked through nvidia-smi when it's installed, since it reports memory exactly.

The answer picks two things: which model fits (models.choose) and which llama.cpp backend to ask for:

* cuda   - NVIDIA cards
* metal  - Apple Silicon Macs
* vulkan - AMD and Intel cards (and NVIDIA when only a Vulkan build is around)
* cpu    - no usable graphics card: every CPU thread through OpenMP
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass

GB = 1024 ** 3
VENDORS = {"10de": "nvidia", "1002": "amd", "1022": "amd", "8086": "intel", "106b": "apple"}
# A graphics card with less memory than this isn't worth putting a model on.
MIN_VRAM_GB = 2.0


@dataclass
class Hardware:
    system: str  # Windows | Linux | Darwin
    cpu_threads: int
    ram_gb: float
    gpu_vendor: str = ""  # nvidia | amd | intel | apple | ""
    gpu_name: str = ""
    vram_gb: float = 0.0  # 0 when unknown or none

    @property
    def backend(self) -> str:
        """The llama.cpp backend that suits this PC best."""
        if self.gpu_vendor == "apple":
            return "metal"
        if self.gpu_vendor == "nvidia" and self.vram_gb >= MIN_VRAM_GB:
            return "cuda"
        if self.gpu_vendor in ("amd", "intel", "nvidia") and self.vram_gb >= MIN_VRAM_GB:
            return "vulkan"
        return "cpu"

    @property
    def has_gpu(self) -> bool:
        return self.backend != "cpu"

    def describe(self) -> str:
        """'AMD Radeon RX 9060 XT (16 GB) · 32 GB RAM · 16 threads'."""
        parts = []
        if self.gpu_name:
            parts.append(self.gpu_name + (f" ({self.vram_gb:.0f} GB)" if self.vram_gb else ""))
        else:
            parts.append("No graphics card found")
        parts.append(f"{self.ram_gb:.0f} GB RAM")
        parts.append(f"{self.cpu_threads} threads")
        return " · ".join(parts)

    def to_json(self) -> dict:
        return asdict(self) | {"backend": self.backend}


# ---------------------------------------------------------------- memory


def ram_bytes() -> int:
    """Total physical memory, or 0 when it can't be read."""
    if sys.platform == "win32":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = MemoryStatus()
        st.dwLength = ctypes.sizeof(MemoryStatus)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullTotalPhys)
        except (AttributeError, OSError):
            return 0
        return 0
    if sys.platform == "darwin":
        out = _quiet(["sysctl", "-n", "hw.memsize"])
        return int(out) if out.isdigit() else 0
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 0


# ---------------------------------------------------------------- graphics cards


def _quiet(cmd: list[str], timeout: float = 8.0) -> str:
    """A small program's output, or "" if it isn't there or fails."""
    from ..ffmpeg import NO_WINDOW

    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=NO_WINDOW).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def nvidia_cards() -> list[tuple[str, float]]:
    """(name, memory in GB) for each NVIDIA card nvidia-smi knows about."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    out = _quiet([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    cards = []
    for line in out.splitlines():
        name, _, mib = line.rpartition(",")
        try:
            cards.append((name.strip(), float(mib) / 1024))
        except ValueError:
            continue
    return cards


DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def windows_cards() -> list[tuple[str, str, float]]:
    """(vendor, name, memory in GB) for each display adapter Windows has a driver for.

    The 64-bit HardwareInformation.qwMemorySize is right for cards over 4 GB; the older 32-bit value
    tops out at 4 GB, so it's only the fallback."""
    try:
        import winreg
    except ImportError:
        return []
    cards = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, DISPLAY_CLASS)
    except OSError:
        return []
    with root:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            if not sub.isdigit():
                continue
            try:
                with winreg.OpenKey(root, sub) as key:
                    values = {}
                    j = 0
                    while True:
                        try:
                            n, v, _ = winreg.EnumValue(key, j)
                        except OSError:
                            break
                        values[n] = v
                        j += 1
            except OSError:
                continue
            name = str(values.get("DriverDesc") or "").strip()
            if not name or "basic display" in name.lower() or "remote" in name.lower():
                continue
            size = _registry_size(values.get("HardwareInformation.qwMemorySize"))
            size = size or _registry_size(values.get("HardwareInformation.MemorySize"))
            ids = str(values.get("MatchingDeviceId") or "").lower()
            m = re.search(r"ven_([0-9a-f]{4})", ids)
            vendor = VENDORS.get(m[1], "") if m else _vendor_from_name(name)
            cards.append((vendor, name, size / GB))
    return cards


def _registry_size(v) -> int:
    if isinstance(v, int):
        return v
    if isinstance(v, (bytes, bytearray)) and v:
        return int.from_bytes(bytes(v[:8]), "little")
    return 0


def _vendor_from_name(name: str) -> str:
    low = name.lower()
    for word, vendor in (("nvidia", "nvidia"), ("geforce", "nvidia"), ("radeon", "amd"), ("amd", "amd"),
                         ("intel", "intel"), ("arc", "intel"), ("apple", "apple")):
        if word in low:
            return vendor
    return ""


def linux_cards(root: str = "/sys/class/drm") -> list[tuple[str, str, float]]:
    """(vendor, name, memory in GB) from /sys. Only AMD's driver says how much memory the card has."""
    cards = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    for entry in entries:
        if not re.fullmatch(r"card\d+", entry):
            continue
        dev = os.path.join(root, entry, "device")
        vendor_id = _read(os.path.join(dev, "vendor")).lower().removeprefix("0x")
        vendor = VENDORS.get(vendor_id, "")
        if not vendor:
            continue
        vram = _read(os.path.join(dev, "mem_info_vram_total"))
        size = int(vram) / GB if vram.isdigit() else 0.0
        name = {"amd": "AMD Radeon", "nvidia": "NVIDIA GPU", "intel": "Intel graphics"}.get(vendor, vendor)
        cards.append((vendor, name, size))
    return cards


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def best_card(cards: list[tuple[str, str, float]]) -> tuple[str, str, float]:
    """The card with the most memory; a dedicated card beats integrated graphics that report none."""
    if not cards:
        return "", "", 0.0
    return max(cards, key=lambda c: (c[2], c[0] == "nvidia", c[0] == "amd"))


# ---------------------------------------------------------------- all together


_cached: Hardware | None = None


def detect(refresh: bool = False) -> Hardware:
    """This PC's hardware (read once, then remembered for the rest of the run)."""
    global _cached
    if _cached is not None and not refresh:
        return _cached
    system = platform.system() or sys.platform
    hw = Hardware(system=system, cpu_threads=os.cpu_count() or 4, ram_gb=round(ram_bytes() / GB, 1))
    if sys.platform == "darwin" and platform.machine() == "arm64":
        # Apple Silicon shares memory with the GPU; macOS lets the GPU use about two thirds of it.
        hw.gpu_vendor, hw.gpu_name, hw.vram_gb = "apple", "Apple Silicon GPU", round(hw.ram_gb * 0.66, 1)
    else:
        nvidia = nvidia_cards()
        cards = [("nvidia", n, g) for n, g in nvidia]
        others = windows_cards() if sys.platform == "win32" else linux_cards()
        cards += [c for c in others if not (c[0] == "nvidia" and nvidia)]  # nvidia-smi's numbers are exact
        hw.gpu_vendor, hw.gpu_name, vram = best_card(cards)
        hw.vram_gb = round(vram, 1)
    _cached = hw
    return hw
