"""What this PC can run a local language model on: memory, CPU threads and the graphics card.

Nothing here starts a program that a virus scanner might flag (no PowerShell, no WMI). Graphics memory
is read, best first, from:

* **NVML** (nvml.dll / libnvidia-ml, NVIDIA's own driver library): exact total and free memory.
* **DXGI** (dxgi.dll, the DirectX layer DirectML runs on), on Windows: every card from every maker, with
  the memory Windows lets a program use right now (its budget) as well as the total.
* **Vulkan**, through the llama-server ClipMonster ships (`--list-devices`): the cards and free memory
  exactly as llama.cpp itself will see them.
* Then the registry (Windows), /sys (Linux), nvidia-smi and sysctl (macOS), which know the total only.

`detect()` reads the PC once per run; `memory_now()` reads free memory again (it changes as other
programs use the card) just before a model is loaded, so llama_engine can size the model to what's free.

The card picks the llama.cpp backend:

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
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

GB = 1024 ** 3
MiB = 1024 ** 2
VENDORS = {"10de": "nvidia", "1002": "amd", "1022": "amd", "8086": "intel", "106b": "apple"}
VENDOR_IDS = {0x10DE: "nvidia", 0x1002: "amd", 0x1022: "amd", 0x8086: "intel", 0x106B: "apple"}
# A graphics card with less memory than this isn't worth putting a model on.
MIN_VRAM_GB = 2.0
FRESH_SECONDS = 10.0  # how long a free-memory reading stays good


@dataclass
class Card:
    """One graphics card, as one source reported it."""

    vendor: str  # nvidia | amd | intel | apple | ""
    name: str
    total_gb: float  # GiB; 0 when unknown
    free_gb: float | None = None  # GiB free right now, None when the source can't say
    source: str = ""  # NVML | DXGI | Vulkan | nvidia-smi | registry | sysfs | macOS
    index: int = -1  # its number in that source (Vulkan0 is 0)
    device: str = ""  # llama.cpp's name for it (Vulkan0, CUDA0), from the Vulkan check


@dataclass
class Hardware:
    system: str  # Windows | Linux | Darwin
    cpu_threads: int
    ram_gb: float
    gpu_vendor: str = ""  # nvidia | amd | intel | apple | ""
    gpu_name: str = ""
    vram_gb: float = 0.0  # 0 when unknown or none
    vram_source: str = ""  # where the total came from
    cards: list[Card] = field(default_factory=list)  # every card any source found

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


@dataclass
class Memory:
    """Free memory right now, for sizing a model to it."""

    vram_total_gb: float
    vram_free_gb: float | None  # None when no source could say (then a share of the total is assumed)
    ram_total_gb: float
    ram_free_gb: float
    source: str = ""  # where the free graphics memory came from
    device: str = ""  # llama.cpp's name for the card, when the Vulkan check found it


# ---------------------------------------------------------------- system memory


def _windows_memory() -> tuple[int, int]:
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
            return int(st.ullTotalPhys), int(st.ullAvailPhys)
    except (AttributeError, OSError):
        pass
    return 0, 0


def ram_bytes() -> int:
    """Total physical memory, or 0 when it can't be read."""
    if sys.platform == "win32":
        return _windows_memory()[0]
    if sys.platform == "darwin":
        out = _quiet(["sysctl", "-n", "hw.memsize"])
        return int(out) if out.isdigit() else 0
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 0


def ram_free_bytes(meminfo: str = "/proc/meminfo") -> int:
    """Memory programs can have right now without anything being swapped out (Linux's MemAvailable,
    Windows' available physical memory). 0 when it can't be read."""
    if sys.platform == "win32":
        return _windows_memory()[1]
    if sys.platform == "darwin":
        out = _quiet(["vm_stat"])
        page = 16384 if platform.machine() == "arm64" else 4096
        m = re.search(r"page size of (\d+) bytes", out)
        if m:
            page = int(m[1])
        pages = 0
        for label in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
            m = re.search(label + r":\s+(\d+)", out)
            pages += int(m[1]) if m else 0
        return pages * page
    try:
        with open(meminfo, encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


# ---------------------------------------------------------------- graphics cards


def _quiet(cmd: list[str], timeout: float = 8.0, env: dict | None = None) -> str:
    """A small program's output, or "" if it isn't there or fails."""
    from ..ffmpeg import NO_WINDOW

    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, encoding="utf-8",
                              errors="replace", creationflags=NO_WINDOW).stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def nvml_cards() -> list[Card]:
    """NVIDIA cards through NVML, the library the driver installs: exact total and free memory."""
    import ctypes

    lib = None
    if sys.platform == "win32":
        names = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvml.dll"),
                 os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "NVIDIA Corporation", "NVSMI",
                              "nvml.dll")]
    elif sys.platform == "darwin":
        return []
    else:
        names = ["libnvidia-ml.so.1", "libnvidia-ml.so"]
    for name in names:
        if os.sep in name and not os.path.isfile(name):
            continue
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError:
            continue
    if lib is None:
        return []

    class NvmlMemory(ctypes.Structure):
        _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]

    try:
        init = getattr(lib, "nvmlInit_v2", None) or lib.nvmlInit
        count_of = getattr(lib, "nvmlDeviceGetCount_v2", None) or lib.nvmlDeviceGetCount
        handle_of = getattr(lib, "nvmlDeviceGetHandleByIndex_v2", None) or lib.nvmlDeviceGetHandleByIndex
        lib.nvmlDeviceGetName.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint]
        lib.nvmlDeviceGetMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(NvmlMemory)]
        handle_of.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
    except AttributeError:
        return []
    if init() != 0:
        return []
    cards = []
    try:
        count = ctypes.c_uint(0)
        if count_of(ctypes.byref(count)) != 0:
            return []
        for i in range(min(count.value, 16)):
            handle = ctypes.c_void_p()
            if handle_of(i, ctypes.byref(handle)) != 0:
                continue
            name = ctypes.create_string_buffer(96)
            lib.nvmlDeviceGetName(handle, name, 96)
            mem = NvmlMemory()
            if lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) != 0:
                continue
            cards.append(Card("nvidia", name.value.decode("utf-8", "replace").strip(), mem.total / GB,
                              mem.free / GB, "NVML", i))
    finally:
        try:
            lib.nvmlShutdown()
        except (AttributeError, OSError):
            pass
    return cards


# DXGI: the DirectX graphics layer every Windows card driver provides (DirectML runs on it). Read with
# ctypes calls into its COM interfaces: method numbers are the interfaces' places in dxgi.h's vtables.
IID_FACTORY1 = "770aae78-f26f-4dba-a829-253c83d1b387"  # IDXGIFactory1
IID_ADAPTER3 = "645967a4-1392-4310-a798-8053ce3e93fd"  # IDXGIAdapter3 (Windows 10 and later)
_RELEASE, _QUERY = 2, 0  # IUnknown
_ENUM_ADAPTERS1 = 12  # IDXGIFactory1::EnumAdapters1
_GET_DESC1 = 10  # IDXGIAdapter1::GetDesc1
_QUERY_VIDEO_MEMORY = 14  # IDXGIAdapter3::QueryVideoMemoryInfo
DXGI_ADAPTER_FLAG_SOFTWARE = 2


def dxgi_cards(include_software: bool = False) -> list[Card]:
    """Every display adapter Windows has, with its own memory and the share Windows lets a program use
    right now (DXGI's budget, which goes down as other programs fill the card)."""
    if sys.platform != "win32":
        return []
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                    ("Data4", ctypes.c_ubyte * 8)]

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]

    class AdapterDesc1(ctypes.Structure):
        _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", ctypes.c_uint), ("DeviceId", ctypes.c_uint),
                    ("SubSysId", ctypes.c_uint), ("Revision", ctypes.c_uint),
                    ("DedicatedVideoMemory", ctypes.c_size_t), ("DedicatedSystemMemory", ctypes.c_size_t),
                    ("SharedSystemMemory", ctypes.c_size_t), ("AdapterLuid", LUID), ("Flags", ctypes.c_uint)]

    class VideoMemoryInfo(ctypes.Structure):
        _fields_ = [("Budget", ctypes.c_uint64), ("CurrentUsage", ctypes.c_uint64),
                    ("AvailableForReservation", ctypes.c_uint64), ("CurrentReservation", ctypes.c_uint64)]

    def guid(text: str) -> GUID:
        u = uuid.UUID(text)
        g = GUID(u.time_low, u.time_mid, u.time_hi_version)
        for i, b in enumerate(u.bytes[8:]):
            g.Data4[i] = b
        return g

    def method(obj: ctypes.c_void_p, index: int, *argtypes):
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
        fn = proto(vtable[index])
        return lambda *args: fn(obj, *args)

    def release(obj: ctypes.c_void_p) -> None:
        if obj:
            method(obj, _RELEASE)()

    try:
        dxgi = ctypes.WinDLL("dxgi")
        create = dxgi.CreateDXGIFactory1
    except (OSError, AttributeError):
        return []
    create.restype = ctypes.c_long
    create.argtypes = [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    factory = ctypes.c_void_p()
    if create(ctypes.byref(guid(IID_FACTORY1)), ctypes.byref(factory)) != 0 or not factory:
        return []
    cards = []
    try:
        enum = method(factory, _ENUM_ADAPTERS1, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))
        for i in range(16):
            adapter = ctypes.c_void_p()
            if enum(i, ctypes.byref(adapter)) != 0 or not adapter:  # DXGI_ERROR_NOT_FOUND: no more
                break
            try:
                desc = AdapterDesc1()
                if method(adapter, _GET_DESC1, ctypes.POINTER(AdapterDesc1))(ctypes.byref(desc)) != 0:
                    continue
                if desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE and not include_software:
                    continue
                total = desc.DedicatedVideoMemory / GB
                free = None
                three = ctypes.c_void_p()
                query = method(adapter, _QUERY, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
                if query(ctypes.byref(guid(IID_ADAPTER3)), ctypes.byref(three)) == 0 and three:
                    try:
                        info = VideoMemoryInfo()
                        ask = method(three, _QUERY_VIDEO_MEMORY, ctypes.c_uint, ctypes.c_int,
                                     ctypes.POINTER(VideoMemoryInfo))
                        if ask(0, 0, ctypes.byref(info)) == 0 and info.Budget:  # node 0, local (on-card) memory
                            budget = min(info.Budget, desc.DedicatedVideoMemory or info.Budget)
                            free = max(0, budget - info.CurrentUsage) / GB
                    finally:
                        release(three)
                vendor = VENDOR_IDS.get(desc.VendorId, "") or _vendor_from_name(desc.Description)
                cards.append(Card(vendor, desc.Description.strip(), total, free, "DXGI", i))
            finally:
                release(adapter)
    finally:
        release(factory)
    return cards


_DEVICE_LINE = re.compile(r"^\s*(?P<dev>[A-Za-z]+\d*):\s*(?P<name>.+?)\s*\((?P<total>\d+)\s*MiB,\s*(?P<free>\d+)\s*MiB free\)")


def parse_devices(text: str) -> list[Card]:
    """llama.cpp's `--list-devices` lines: '  Vulkan0: AMD Radeon RX 9060 XT (16304 MiB, 15890 MiB free)'."""
    cards = []
    for line in text.splitlines():
        m = _DEVICE_LINE.match(line)
        if not m:
            continue
        dev, name = m["dev"], m["name"].strip()
        idx = re.search(r"(\d+)$", dev)
        cards.append(Card(_vendor_from_name(name), name, int(m["total"]) * MiB / GB, int(m["free"]) * MiB / GB,
                          "Vulkan" if dev.lower().startswith("vulkan") else re.sub(r"\d+$", "", dev),
                          int(idx[1]) if idx else -1, dev))
    return cards


def vulkan_cards(exe: Path | None, timeout: float = 20.0) -> list[Card]:
    """The cards llama.cpp itself sees, through the llama-server ClipMonster runs (Vulkan in the release
    build): its free memory is Vulkan's memory budget, which is what the model will actually get."""
    if exe is None or not Path(exe).is_file():
        return []
    return parse_devices(_quiet([str(exe), "--list-devices"], timeout=timeout))


def nvidia_cards() -> list[Card]:
    """NVIDIA cards from nvidia-smi, for when NVML can't be loaded."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    out = _quiet([exe, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"])
    cards = []
    for i, line in enumerate(out.splitlines()):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            total = float(parts[1]) / 1024
            free = float(parts[2]) / 1024 if len(parts) > 2 else None
        except ValueError:
            continue
        cards.append(Card("nvidia", parts[0], total, free, "nvidia-smi", i))
    return cards


DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def windows_cards() -> list[Card]:
    """Each display adapter Windows has a driver for, from the registry (total memory only).

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
            cards.append(Card(vendor, name, size / GB, None, "registry", int(sub)))
    return cards


def _registry_size(v) -> int:
    if isinstance(v, int):
        return v
    if isinstance(v, (bytes, bytearray)) and v:
        return int.from_bytes(bytes(v[:8]), "little")
    return 0


def _vendor_from_name(name: str) -> str:
    low = name.lower()
    for word, vendor in (("nvidia", "nvidia"), ("geforce", "nvidia"), ("quadro", "nvidia"), ("radeon", "amd"),
                         ("amd", "amd"), ("intel", "intel"), ("arc", "intel"), ("apple", "apple")):
        if re.search(r"\b" + word + r"\b", low):
            return vendor
    return ""


def linux_cards(root: str = "/sys/class/drm") -> list[Card]:
    """Cards from /sys. Only AMD's driver says how much memory the card has (and how much is in use)."""
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
        total = _read(os.path.join(dev, "mem_info_vram_total"))
        used = _read(os.path.join(dev, "mem_info_vram_used"))
        size = int(total) / GB if total.isdigit() else 0.0
        free = (int(total) - int(used)) / GB if total.isdigit() and used.isdigit() else None
        name = {"amd": "AMD Radeon", "nvidia": "NVIDIA GPU", "intel": "Intel graphics"}.get(vendor, vendor)
        cards.append(Card(vendor, name, size, free, "sysfs", int(entry[4:])))
    return cards


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def best_card(cards: list[Card]) -> Card | None:
    """The card with the most memory; a dedicated card beats integrated graphics that report none."""
    if not cards:
        return None
    return max(cards, key=lambda c: (c.total_gb, c.vendor == "nvidia", c.vendor == "amd"))


def _words(name: str) -> set[str]:
    stop = {"nvidia", "amd", "intel", "geforce", "radeon", "graphics", "gpu", "tm", "r", "the", "corporation"}
    return {w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in stop}


def same_card(a: Card, b: Card) -> bool:
    """Whether two sources are talking about the same card: same maker and the same model words (Vulkan
    adds a driver name in brackets, Windows doesn't), or the same memory when a name is generic."""
    if a.vendor and b.vendor and a.vendor != b.vendor:
        return False
    wa, wb = _words(re.sub(r"\(.*?\)", "", a.name)), _words(re.sub(r"\(.*?\)", "", b.name))
    if wa and wb and (wa <= wb or wb <= wa):
        return True
    if a.total_gb and b.total_gb:
        return abs(a.total_gb - b.total_gb) <= max(0.6, 0.06 * max(a.total_gb, b.total_gb))
    return False


# ---------------------------------------------------------------- all together


_cached: Hardware | None = None
_lock = threading.Lock()


def detect(refresh: bool = False) -> Hardware:
    """This PC's hardware (read once, then remembered for the rest of the run)."""
    global _cached
    with _lock:
        if _cached is not None and not refresh:
            return _cached
        system = platform.system() or sys.platform
        hw = Hardware(system=system, cpu_threads=os.cpu_count() or 4, ram_gb=round(ram_bytes() / GB, 1))
        if sys.platform == "darwin" and platform.machine() == "arm64":
            # Apple Silicon shares memory with the GPU; macOS lets the GPU use about two thirds of it.
            card = Card("apple", "Apple Silicon GPU", round(hw.ram_gb * 0.66, 1), None, "macOS")
            hw.cards = [card]
        else:
            nvidia = nvml_cards() or nvidia_cards()
            if sys.platform == "win32":
                others = dxgi_cards() or windows_cards()
            else:
                others = linux_cards()
            # NVML's and nvidia-smi's numbers are exact; another source's NVIDIA card is the same one.
            hw.cards = nvidia + [c for c in others if not (c.vendor == "nvidia" and nvidia)]
            card = best_card(hw.cards)
        if card is not None:
            hw.gpu_vendor, hw.gpu_name, hw.vram_gb, hw.vram_source = (card.vendor, card.name,
                                                                      round(card.total_gb, 1), card.source)
        _cached = hw
        return hw


_memory: tuple[float, tuple, Memory] | None = None


def memory_now(hw: Hardware | None = None, exe: Path | None = None, refresh: bool = False) -> Memory:
    """Free graphics memory on the card the model would use, and free RAM, read now (a reading is kept
    for a few seconds). `exe`, a llama-server, adds its Vulkan check: the free memory llama.cpp sees."""
    global _memory
    hw = hw or detect()
    key = (hw.gpu_name, str(exe or ""))
    now = time.monotonic()
    if not refresh and _memory is not None and _memory[1] == key and now - _memory[0] < FRESH_SECONDS:
        return _memory[2]
    ram_total = hw.ram_gb
    ram_free = ram_free_bytes() / GB or ram_total * 0.5
    mem = Memory(hw.vram_gb, None, ram_total, round(ram_free, 2))
    if hw.has_gpu:
        target = Card(hw.gpu_vendor, hw.gpu_name, hw.vram_gb)
        fresh: list[Card] = []
        if hw.gpu_vendor == "nvidia":
            fresh += nvml_cards() or nvidia_cards()
        if sys.platform == "win32":
            fresh += dxgi_cards()
        elif hw.vram_source == "sysfs":
            fresh += linux_cards()
        vulkan = vulkan_cards(exe)
        for card in vulkan:
            if same_card(card, target):
                mem.device = card.device
                break
        # NVML first (exact), then the Vulkan check (what llama.cpp gets), then DXGI and /sys.
        order = {"NVML": 0, "Vulkan": 1, "nvidia-smi": 2, "DXGI": 3, "sysfs": 4}
        for card in sorted(fresh + vulkan, key=lambda c: order.get(c.source, 9)):
            if card.free_gb is not None and same_card(card, target):
                mem.vram_free_gb, mem.source = round(min(card.free_gb, hw.vram_gb or card.free_gb), 2), card.source
                break
    _memory = (now, key, mem)
    return mem
