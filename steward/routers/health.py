import socket

from fastapi import APIRouter, Request

from steward import __version__
from steward.services import ingress_caddy
from steward.services.code_hash import compute_agent_hash
from steward.services.ingress_caddy import CaddyError
from steward.services.system_metrics import host_metrics


router = APIRouter()


_cached_hash: str | None = None


def _get_hash() -> str:
    global _cached_hash
    if _cached_hash is None:
        _cached_hash = compute_agent_hash()
    return _cached_hash


def _caddy_snapshot() -> dict:
    try:
        routes = ingress_caddy.list_managed_routes()
    except CaddyError as exc:
        return {'ok': False, 'error': str(exc), 'route_count': 0, 'routes': {}, 'srv0_raw': None}
    srv0_raw = None
    try:
        srv0_raw = ingress_caddy.get_server_config()
    except CaddyError:
        pass
    return {
        'ok': True,
        'route_count': len(routes),
        'routes': routes,
        'srv0_raw': srv0_raw,
    }


@router.get('/v1/health')
def health(request: Request) -> dict:
    config = request.app.state.config
    return {
        'ok': True,
        'version': __version__,
        'agent_hash': _get_hash(),
        'hostname': socket.gethostname(),
        'tailscale_ip': config.bind_host,
        'metrics': host_metrics(),
        'caddy': _caddy_snapshot(),
    }
