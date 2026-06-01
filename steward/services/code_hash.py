import hashlib
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parent.parent.parent


EXCLUDE_DIRS = {'__pycache__', '.venv', '.git', 'node_modules'}


def compute_agent_hash() -> str:
    h = hashlib.sha256()
    if not AGENT_ROOT.exists():
        return ''
    for p in sorted(AGENT_ROOT.rglob('*')):
        if p.is_dir():
            continue
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        if p.suffix == '.pyc':
            continue
        rel = p.relative_to(AGENT_ROOT).as_posix()
        h.update(rel.encode('utf-8'))
        h.update(b'\0')
        try:
            h.update(p.read_bytes())
        except OSError:
            continue
        h.update(b'\0')
    return h.hexdigest()
