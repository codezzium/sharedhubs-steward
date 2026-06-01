import json
import os
import socket
import subprocess
import tempfile


class ProxmoxError(RuntimeError):
    pass


def _run(args: list[str], timeout: float = 120.0) -> str:
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=timeout,
    )
    if result.returncode != 0:
        raise ProxmoxError(f"{' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def vm_clone(template_vmid: int, new_vmid: int, name: str) -> None:
    _run(['qm', 'clone', str(template_vmid), str(new_vmid), '--name', name])


def vm_configure(
    *,
    vmid: int,
    cores: int,
    memory_mb: int,
    bridge: str,
    vm_ip: str,
    gateway: str,
    prefix: int,
    nameserver: str,
    ssh_pubkey: str,
) -> None:
    _run([
        'qm', 'set', str(vmid),
        '--cores', str(cores),
        '--memory', str(memory_mb),
        '--net0', f'virtio,bridge={bridge}',
        '--ipconfig0', f'ip={vm_ip}/{prefix},gw={gateway}',
        '--nameserver', nameserver,
        '--ciuser', 'root',
    ])
    with tempfile.NamedTemporaryFile('w', delete=False, suffix='.pub', encoding='utf-8') as f:
        f.write(ssh_pubkey.strip() + '\n')
        key_path = f.name
    try:
        _run(['qm', 'set', str(vmid), '--sshkey', key_path])
    finally:
        try:
            os.unlink(key_path)
        except OSError:
            pass


def vm_resize_disk(vmid: int, disk_gb: int) -> None:
    _run(['qm', 'resize', str(vmid), 'scsi0', f'{disk_gb}G'])


def vm_start(vmid: int) -> None:
    _run(['qm', 'start', str(vmid)])


def vm_stop(vmid: int) -> None:
    _run(['qm', 'stop', str(vmid)])


def vm_destroy(vmid: int) -> None:
    try:
        _run(['qm', 'destroy', str(vmid), '--purge', '1'])
    except ProxmoxError as exc:
        # Idempotent: VM zaten yoksa istenen son durum sağlanmış — sessizce geç.
        if 'does not exist' in str(exc).lower():
            return
        raise


def vm_status(vmid: int) -> str:
    out = _run(['qm', 'status', str(vmid)])
    return out.strip().split(':', 1)[-1].strip()


def vm_resources(vmid: int) -> dict:
    """Return runtime resource snapshot via pvesh + qemu-guest-agent.

    Returns dict with at least:
      cpu_percent, cpu_count, mem_used_bytes, mem_total_bytes, mem_percent,
      disk_alloc_bytes, uptime_seconds, status

    If qemu-guest-agent is installed and reachable inside the VM, also includes
    real guest-side disk usage:
      disk_used_bytes, disk_total_bytes, disk_percent  (from "/" mountpoint)

    On error returns an empty dict — caller should treat metrics as missing.
    """
    try:
        out = _run(
            ['pvesh', 'get', f'/nodes/{socket.gethostname()}/qemu/{vmid}/status/current',
             '--output-format', 'json'],
            timeout=10.0,
        )
        data = json.loads(out)
    except (ProxmoxError, ValueError, subprocess.TimeoutExpired):
        return {}

    cpus = int(data.get('cpus') or 1)
    cpu_frac = float(data.get('cpu') or 0.0)
    mem_total = int(data.get('maxmem') or 0)
    mem_used = int(data.get('mem') or 0)
    mem_pct = (mem_used / mem_total * 100) if mem_total else 0.0
    result = {
        'status': data.get('status', 'unknown'),
        'cpu_percent': round(cpu_frac * 100, 1),
        'cpu_count': cpus,
        'mem_used_bytes': mem_used,
        'mem_total_bytes': mem_total,
        'mem_percent': round(mem_pct, 1),
        'disk_alloc_bytes': int(data.get('maxdisk') or 0),
        'uptime_seconds': int(data.get('uptime') or 0),
    }

    fs = _vm_root_fs_usage(vmid)
    if fs is not None:
        used, total = fs
        result['disk_used_bytes'] = used
        result['disk_total_bytes'] = total
        result['disk_percent'] = round((used / total * 100) if total else 0.0, 1)

    return result


def vm_guest_exec(vmid: int, command: list[str], timeout: float = 180.0) -> dict:
    args = ['qm', 'guest', 'exec', str(vmid)]
    args += ['--timeout', str(int(timeout))]
    args += ['--'] + command
    out = _run(args, timeout=timeout + 10.0)
    try:
        data = json.loads(out)
    except ValueError as exc:
        raise ProxmoxError(f'guest exec returned invalid JSON: {out!r}') from exc
    return {
        'exitcode': int(data.get('exitcode', -1)),
        'stdout': (data.get('out-data') or '').strip(),
        'stderr': (data.get('err-data') or '').strip(),
        'exited': bool(data.get('exited', True)),
    }


def _vm_root_fs_usage(vmid: int) -> tuple[int, int] | None:
    """Query qemu-guest-agent for root filesystem usage. Returns (used, total)
    bytes or None if guest agent is unavailable/unresponsive.
    """
    try:
        out = _run(
            ['qm', 'agent', str(vmid), 'get-fsinfo'],
            timeout=5.0,
        )
    except (ProxmoxError, subprocess.TimeoutExpired):
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    root = None
    for entry in data:
        if entry.get('mountpoint') == '/':
            root = entry
            break
    if root is None and data:
        root = data[0]
    if root is None:
        return None
    used = root.get('used-bytes')
    total = root.get('total-bytes')
    if used is None or total is None:
        return None
    try:
        return int(used), int(total)
    except (TypeError, ValueError):
        return None
