import os
from dataclasses import dataclass
from pathlib import Path

import yaml


DEFAULT_CONFIG_PATH = Path('/etc/steward/config.yml')


@dataclass
class AgentConfig:
    bind_host: str
    bind_port: int
    panel_url: str
    pve_storage: str
    template_vmid: int
    agent_token: str = ''
    config_path: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> 'AgentConfig':
        path = path or Path(os.environ.get('STEWARD_CONFIG', DEFAULT_CONFIG_PATH))
        with path.open('r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        return cls(
            bind_host=data['bind_host'],
            bind_port=int(data.get('bind_port', 9090)),
            panel_url=data['panel_url'].rstrip('/'),
            pve_storage=data.get('pve_storage', 'local-lvm'),
            template_vmid=int(data.get('template_vmid', 9000)),
            agent_token=str(data.get('agent_token', '') or ''),
            config_path=path,
        )

    def reload_token(self) -> str:
        if not self.config_path:
            return self.agent_token
        try:
            with self.config_path.open('r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            self.agent_token = str(data.get('agent_token', '') or '')
        except OSError:
            pass
        return self.agent_token
