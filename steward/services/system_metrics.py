import os
import subprocess

import psutil


_PSEUDO_FSTYPES = frozenset({
    '', 'tmpfs', 'devtmpfs', 'devfs', 'proc', 'sysfs', 'cgroup', 'cgroup2',
    'overlay', 'overlay2', 'squashfs', 'aufs', 'autofs', 'fuse.lxcfs',
    'rpc_pipefs', 'binfmt_misc', 'mqueue', 'pstore', 'efivarfs', 'bpf',
    'tracefs', 'debugfs', 'configfs', 'fusectl', 'hugetlbfs', 'securityfs',
    'nsfs', 'ramfs',
})


_PHYSICAL_DISK_SKIP_PREFIXES = (
    'loop', 'ram', 'sr', 'scd', 'zram', 'dm-', 'md',
)

_LOCAL_PVE_STORAGE_TYPES = frozenset({
    'dir', 'lvm', 'lvmthin', 'zfspool', 'btrfs', 'zfs',
})


def _proxmox_storage_stats() -> tuple[int, int]:
    """Sum total + used bytes across active local Proxmox storages.

    Uses `pvesm status`. Skips network storages (nfs, cifs, cephfs, etc.)
    because those don't reflect host disk pressure. Returns (0, 0) if
    pvesm isn't available or the call fails — caller falls back to
    /sys/block + filesystem aggregation.

    pvesm output columns: Name Type Status Total Used Available %
    Sizes are reported in 1K blocks.
    """
    try:
        result = subprocess.run(
            ['pvesm', 'status'],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return 0, 0
    if result.returncode != 0:
        return 0, 0

    total = 0
    used = 0
    lines = result.stdout.strip().splitlines()
    if len(lines) < 2:
        return 0, 0
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        stype = parts[1].lower()
        status = parts[2].lower()
        if status != 'active':
            continue
        if stype not in _LOCAL_PVE_STORAGE_TYPES:
            continue
        try:
            t = int(parts[3]) * 1024
            u = int(parts[4]) * 1024
        except (ValueError, IndexError):
            continue
        total += t
        used += u
    return total, used


def _physical_disk_total() -> int:
    """Sum sizes of whole physical block devices via /sys/block.

    Skips loop/ram/cd/dm/md (logical layers above physical disks) so we
    count raw hardware capacity once. Returns 0 on non-Linux or any read
    failure, in which case the caller falls back to filesystem aggregation.
    """
    sys_block = '/sys/block'
    if not os.path.isdir(sys_block):
        return 0
    total = 0
    try:
        names = os.listdir(sys_block)
    except OSError:
        return 0
    for name in names:
        if any(name.startswith(p) for p in _PHYSICAL_DISK_SKIP_PREFIXES):
            continue
        size_path = os.path.join(sys_block, name, 'size')
        try:
            with open(size_path) as f:
                sectors = int(f.read().strip())
        except (OSError, ValueError):
            continue
        total += sectors * 512
    return total


def _aggregate_disk_used() -> int:
    """Sum used bytes across all real local mount points.

    Used as the proxy for "consumed disk" — VM-allocated LVM-thin volumes
    aren't visible at the filesystem level, so this under-reports on
    Proxmox; good enough for showing root/data partition fill.
    """
    seen_devices: set[str] = set()
    used = 0
    for part in psutil.disk_partitions(all=False):
        if (part.fstype or '').lower() in _PSEUDO_FSTYPES:
            continue
        if part.device and part.device in seen_devices:
            continue
        if part.device:
            seen_devices.add(part.device)
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        used += usage.used
    return used


def _aggregate_disk_fallback() -> tuple[int, int]:
    """If /sys/block isn't readable, fall back to filesystem totals."""
    seen_devices: set[str] = set()
    total = 0
    used = 0
    for part in psutil.disk_partitions(all=False):
        if (part.fstype or '').lower() in _PSEUDO_FSTYPES:
            continue
        if part.device and part.device in seen_devices:
            continue
        if part.device:
            seen_devices.add(part.device)
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        total += usage.total
        used += usage.used
    return total, used


def host_metrics() -> dict:
    """Return current host resource snapshot.

    cpu_percent uses interval=None which is non-blocking; the very first call
    in this process returns 0.0, subsequent calls report the delta since the
    last call. Heartbeat polls every 30s so values stabilize quickly.

    disk_* fields aggregate every real local mount (root, VM storage, data
    partitions) so the panel sees the whole machine's storage, not just /.
    """
    mem = psutil.virtual_memory()
    # Priority: pvesm (Proxmox storage-aware, includes LVM-thin used) >
    # /sys/block raw physical (sees full disks, ignores RAID overhead) >
    # filesystem aggregation (worst case, only mounted FS).
    disk_total, disk_used = _proxmox_storage_stats()
    if disk_total == 0:
        disk_total = _physical_disk_total()
        if disk_total > 0:
            disk_used = _aggregate_disk_used()
        else:
            disk_total, disk_used = _aggregate_disk_fallback()
    disk_percent = round(disk_used / disk_total * 100, 1) if disk_total else 0.0
    try:
        load1, _load5, _load15 = os.getloadavg()
    except (OSError, AttributeError):
        load1 = 0.0
    return {
        'cpu_percent': round(psutil.cpu_percent(interval=None), 1),
        'cpu_count': psutil.cpu_count(logical=True) or 1,
        'load_1m': round(load1, 2),
        'mem_used_bytes': mem.total - mem.available,
        'mem_total_bytes': mem.total,
        'mem_percent': round(mem.percent, 1),
        'disk_used_bytes': disk_used,
        'disk_total_bytes': disk_total,
        'disk_percent': disk_percent,
    }
