import subprocess


PERSIST_PATH = '/etc/iptables/rules.v4'


class IptablesError(RuntimeError):
    pass


def _run(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise IptablesError(f"{' '.join(args)} failed: {result.stderr.strip()}")


def add_ssh_forward(host_port: int, vm_ip: str, vm_port: int = 22) -> None:
    rule = ['iptables', '-t', 'nat', '-A', 'PREROUTING',
            '-p', 'tcp', '--dport', str(host_port),
            '-j', 'DNAT', '--to-destination', f'{vm_ip}:{vm_port}']
    check = ['iptables', '-t', 'nat', '-C', 'PREROUTING',
             '-p', 'tcp', '--dport', str(host_port),
             '-j', 'DNAT', '--to-destination', f'{vm_ip}:{vm_port}']
    if subprocess.run(check, capture_output=True).returncode != 0:
        _run(rule)
    persist()


def remove_ssh_forward(host_port: int, vm_ip: str, vm_port: int = 22) -> None:
    rule = ['iptables', '-t', 'nat', '-D', 'PREROUTING',
            '-p', 'tcp', '--dport', str(host_port),
            '-j', 'DNAT', '--to-destination', f'{vm_ip}:{vm_port}']
    check = ['iptables', '-t', 'nat', '-C', 'PREROUTING',
             '-p', 'tcp', '--dport', str(host_port),
             '-j', 'DNAT', '--to-destination', f'{vm_ip}:{vm_port}']
    if subprocess.run(check, capture_output=True).returncode == 0:
        _run(rule)
    persist()


def persist() -> None:
    with open(PERSIST_PATH, 'w', encoding='utf-8') as f:
        result = subprocess.run(['iptables-save'], capture_output=True, text=True)
        if result.returncode != 0:
            raise IptablesError(f'iptables-save failed: {result.stderr.strip()}')
        f.write(result.stdout)
