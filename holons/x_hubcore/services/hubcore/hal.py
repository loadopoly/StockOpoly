"""Part 11.3 - Dynamic Multi-Tier Hardware Conductor (Hardware Abstraction Layer).

Auto-discovers host CPU, RAM, and GPU topology at startup and selects a
Conductor execution tier. The Hardware Upgradability Invariant is satisfied by
probing on every process start: swapping a GPU or adding RAM changes the tier
without a code edit or container rebuild.

Tier selection (Part 11.3 table):

    Tier 0  CPU-only or <=16 GB RAM        serialized queues, CPU inference
    Tier 1  single GPU below 16 GB VRAM    at most one heavy model resident
    Tier 2  multi-GPU or >=16 GB VRAM      simultaneous resident fleet
    Tier 3  heterogeneous accelerators     affinity routing per model class

The <=4 GB per-model VRAM budget (decision 0.3.11) applies on every tier; Tier 2
and Tier 3 relax only *concurrency*, never per-model residency size.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

#: Decision 0.3.11 - every supported profile stays functional at this budget.
HEAVY_MODEL_VRAM_BUDGET_MB = 4096

TIER_CPU_ONLY = 0
TIER_SINGLE_GPU = 1
TIER_CONCURRENT_POOL = 2
TIER_ACCELERATOR_MATRIX = 3

#: A single GPU with at least this much VRAM is treated as a workstation pool.
TIER2_VRAM_THRESHOLD_MB = 16384

#: Below this much system RAM the host degrades to CPU-only serialized queues.
TIER0_RAM_FLOOR_GB = 16


@dataclass
class GpuDevice:
    """One discovered accelerator."""

    index: int
    name: str
    vram_total_mb: int
    vendor: str = "unknown"

    def can_host_heavy_model(self, headroom_mb: int = 1024) -> bool:
        return self.vram_total_mb >= HEAVY_MODEL_VRAM_BUDGET_MB + headroom_mb


@dataclass
class HardwareProfile:
    """The discovered topology plus the tier and budgets derived from it."""

    hostname: str
    cpu_count: int
    ram_gb: float
    gpus: List[GpuDevice] = field(default_factory=list)
    tier: int = TIER_CPU_ONLY
    tier_name: str = "cpu-only"
    concurrent_resident_models: int = 1
    heavy_model_budget_mb: int = HEAVY_MODEL_VRAM_BUDGET_MB
    worker_queue_depth: int = 2
    affinity: Dict[str, str] = field(default_factory=dict)

    @property
    def total_vram_mb(self) -> int:
        return sum(gpu.vram_total_mb for gpu in self.gpus)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["total_vram_mb"] = self.total_vram_mb
        return payload


def _probe_ram_gb() -> float:
    """Best-effort physical RAM discovery without a hard psutil dependency."""
    try:
        import psutil  # type: ignore

        return round(psutil.virtual_memory().total / (1024 ** 3), 2)
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(MemoryStatusEx)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return round(status.ullTotalPhys / (1024 ** 3), 2)
        except Exception:
            return 0.0
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round(pages * page_size / (1024 ** 3), 2)
    except Exception:
        return 0.0


def _probe_nvidia() -> List[GpuDevice]:
    """Discover NVIDIA devices via NVML, falling back to the nvidia-smi CLI."""
    devices: List[GpuDevice] = []
    try:
        import pynvml  # type: ignore

        pynvml.nvmlInit()
        for index in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            devices.append(
                GpuDevice(index, name, int(info.total // (1024 * 1024)), "nvidia")
            )
        pynvml.nvmlShutdown()
        return devices
    except Exception:
        pass

    binary = shutil.which("nvidia-smi")
    if not binary:
        return devices
    try:
        output = subprocess.run(
            [binary, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout
    except Exception:
        return devices
    for index, line in enumerate(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        name, _, vram = line.rpartition(",")
        try:
            devices.append(GpuDevice(index, name.strip(), int(float(vram.strip())), "nvidia"))
        except ValueError:
            continue
    return devices


def _probe_torch() -> List[GpuDevice]:
    """Cover ROCm, DirectML, and Apple MPS through whatever torch reports."""
    try:
        import torch  # type: ignore
    except Exception:
        return []
    devices: List[GpuDevice] = []
    try:
        if torch.cuda.is_available():
            for index in range(torch.cuda.device_count()):
                props = torch.cuda.get_device_properties(index)
                vendor = "amd" if getattr(torch.version, "hip", None) else "nvidia"
                devices.append(
                    GpuDevice(index, props.name, int(props.total_memory // (1024 * 1024)), vendor)
                )
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            devices.append(GpuDevice(0, "Apple MPS", 0, "apple"))
    except Exception:
        return devices
    return devices


def discover_gpus() -> List[GpuDevice]:
    """Discover accelerators, preferring the most detailed source available."""
    devices = _probe_nvidia()
    if not devices:
        devices = _probe_torch()
    return devices


def _classify(cpu_count: int, ram_gb: float, gpus: List[GpuDevice]) -> tuple:
    """Return (tier, tier_name) for a discovered topology."""
    usable = [gpu for gpu in gpus if gpu.vram_total_mb > 0]
    if not usable or ram_gb and ram_gb < TIER0_RAM_FLOOR_GB:
        return TIER_CPU_ONLY, "cpu-only"
    vendors = {gpu.vendor for gpu in usable}
    if len(usable) > 1 and len(vendors) > 1:
        return TIER_ACCELERATOR_MATRIX, "accelerator-matrix"
    if len(usable) > 1 or max(gpu.vram_total_mb for gpu in usable) >= TIER2_VRAM_THRESHOLD_MB:
        return TIER_CONCURRENT_POOL, "concurrent-workstation-pool"
    return TIER_SINGLE_GPU, "single-gpu-conductor"


def _affinity_for(tier: int, gpus: List[GpuDevice]) -> Dict[str, str]:
    """Map the three architectural axes onto discovered devices.

    On Tier 0 and Tier 1 every axis shares the one execution slot, so the
    Conductor serializes. On Tier 2 and Tier 3 the axes are pinned round-robin
    across devices (Part 11.4 "Multi-GPU / High-VRAM Allocation" column).
    """
    axes = ("entirety", "vision", "touch")
    if tier in (TIER_CPU_ONLY,) or not gpus:
        return {axis: "cpu" for axis in axes}
    if tier == TIER_SINGLE_GPU:
        return {axis: "gpu:0" for axis in axes}
    return {axis: "gpu:" + str(gpus[i % len(gpus)].index) for i, axis in enumerate(axes)}


def probe(workspace: Optional[Dict[str, Any]] = None) -> HardwareProfile:
    """Probe the host and derive the active Conductor profile.

    A workspace document may pin the tier via ``hardware.profile``; ``auto``
    (the default) always re-derives from the live topology.
    """
    import socket

    cpu_count = os.cpu_count() or 1
    ram_gb = _probe_ram_gb()
    gpus = discover_gpus()

    hardware = (workspace or {}).get("hardware", {}) if workspace else {}
    headroom = int(hardware.get("vramHeadroomMb", 1024) or 1024)
    pinned = str(hardware.get("profile", "auto") or "auto")

    tier, tier_name = _classify(cpu_count, ram_gb, gpus)
    if pinned != "auto":
        overrides = {
            "cpu-only": (TIER_CPU_ONLY, "cpu-only"),
            "single-gpu": (TIER_SINGLE_GPU, "single-gpu-conductor"),
            "multi-gpu": (TIER_CONCURRENT_POOL, "concurrent-workstation-pool"),
            "accelerator-pool": (TIER_ACCELERATOR_MATRIX, "accelerator-matrix"),
        }
        tier, tier_name = overrides.get(pinned, (tier, tier_name))

    if tier == TIER_CPU_ONLY:
        concurrent, queue_depth = 1, max(1, cpu_count // 4)
    elif tier == TIER_SINGLE_GPU:
        concurrent, queue_depth = 1, max(2, cpu_count // 2)
    else:
        hostable = sum(1 for gpu in gpus if gpu.can_host_heavy_model(headroom))
        concurrent, queue_depth = max(1, hostable), max(4, cpu_count)

    return HardwareProfile(
        hostname=socket.gethostname(),
        cpu_count=cpu_count,
        ram_gb=ram_gb,
        gpus=gpus,
        tier=tier,
        tier_name=tier_name,
        concurrent_resident_models=concurrent,
        heavy_model_budget_mb=HEAVY_MODEL_VRAM_BUDGET_MB,
        worker_queue_depth=queue_depth,
        affinity=_affinity_for(tier, gpus),
    )


def admits_resident(profile: HardwareProfile, currently_resident: int) -> bool:
    """Conductor admission control.

    On Tier 1 this enforces "at most one heavy model resident"; the caller must
    unload before loading another (Part 11.3 residency rule).
    """
    return currently_resident < profile.concurrent_resident_models


def as_json(profile: HardwareProfile) -> str:
    return json.dumps(profile.to_dict(), indent=2)
