import json
import logging
import re
import urllib.error
import urllib.request


logger = logging.getLogger(__name__)

CADDY_ADMIN = 'http://localhost:2019'
CADDY_SERVER = 'srv0'
ROUTE_ID_PREFIX = 'shproject-'
ROUTE_ID_RE = re.compile(r'^shproject-[A-Za-z0-9.\-]{1,253}$')


class CaddyError(RuntimeError):
    pass


def caddy_request(method: str, path: str, body: bytes | None = None, timeout: float = 5.0) -> tuple[int, bytes]:
    url = f'{CADDY_ADMIN}{path}'
    headers = {'Content-Type': 'application/json'} if body else {}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        body_bytes = b''
        try:
            body_bytes = exc.read()
        except Exception:
            pass
        return exc.code, body_bytes
    except urllib.error.URLError as exc:
        raise CaddyError(f'Caddy admin unreachable: {exc.reason}') from exc


def route_id(hostname: str) -> str:
    return f'{ROUTE_ID_PREFIX}{hostname}'


def build_route(hostname: str, upstream_dial: str) -> dict:
    return {
        '@id': route_id(hostname),
        'match': [{'host': [hostname]}],
        'handle': [
            {
                'handler': 'reverse_proxy',
                'upstreams': [{'dial': upstream_dial}],
                'headers': {
                    'request': {
                        'set': {
                            'Host': ['{http.request.host}'],
                            'X-Forwarded-Proto': ['https'],
                            'X-Forwarded-Host': ['{http.request.host}'],
                            'X-Real-IP': ['{http.request.remote.host}'],
                        },
                    },
                },
            },
        ],
    }


def upsert_route(hostname: str, upstream_dial: str) -> None:
    hostname = hostname.lower()
    rid = route_id(hostname)
    del_status, _ = caddy_request('DELETE', f'/id/{rid}')
    if del_status not in (200, 404):
        raise CaddyError(f'caddy delete (pre-upsert) failed: HTTP {del_status}')

    body = json.dumps(build_route(hostname, upstream_dial)).encode('utf-8')
    ins_status, ins_body = caddy_request(
        'POST',
        f'/config/apps/http/servers/{CADDY_SERVER}/routes/0',
        body,
    )
    if ins_status not in (200, 201):
        raise CaddyError(f'caddy insert failed: HTTP {ins_status} body={ins_body[:200]!r}')


def delete_route(hostname: str) -> bool:
    hostname = hostname.lower()
    rid = route_id(hostname)
    status, _ = caddy_request('DELETE', f'/id/{rid}')
    if status not in (200, 404):
        raise CaddyError(f'caddy delete failed: HTTP {status}')
    return status == 200


def _prune_stray_routes() -> None:
    r_st, r_body = caddy_request('GET', f'/config/apps/http/servers/{CADDY_SERVER}/routes')
    if r_st != 200:
        return
    try:
        routes = json.loads(r_body.decode('utf-8'))
    except Exception:
        return
    if not isinstance(routes, list):
        return
    kept = [
        r for r in routes
        if isinstance(r, dict) and str(r.get('@id', '')).startswith(ROUTE_ID_PREFIX)
    ]
    if len(kept) != len(routes):
        caddy_request(
            'PATCH', f'/config/apps/http/servers/{CADDY_SERVER}/routes',
            json.dumps(kept).encode('utf-8'),
        )
        logger.info('configure_caddy_mode: pruned %d stray route(s)', len(routes) - len(kept))


def _free_listeners(servers: dict, ports: tuple, keep: str) -> None:
    for name, srv in servers.items():
        if name == keep:
            continue
        srv_listen = srv.get('listen') or []
        if any(p in srv_listen for p in ports):
            caddy_request('DELETE', f'/config/apps/http/servers/{name}')


def _automatic_https_disabled() -> bool:
    st, body = caddy_request('GET', f'/config/apps/http/servers/{CADDY_SERVER}/automatic_https')
    if st != 200:
        return False
    try:
        ah = json.loads(body.decode('utf-8'))
    except Exception:
        return False
    return isinstance(ah, dict) and ah.get('disable') is True


def configure_caddy_mode(public: bool) -> bool:
    status, body = caddy_request('GET', '/config/apps/http/servers')
    if status != 200:
        return False
    try:
        servers = json.loads(body.decode('utf-8'))
    except Exception:
        return False
    if not isinstance(servers, dict) or CADDY_SERVER not in servers:
        return False
    listen = sorted((servers[CADDY_SERVER].get('listen') or []))

    if public:
        if _automatic_https_disabled():
            caddy_request('DELETE', f'/config/apps/http/servers/{CADDY_SERVER}/automatic_https')
        want = [':443', ':80']
        if listen != sorted(want):
            _free_listeners(servers, (':80', ':443'), CADDY_SERVER)
            caddy_request(
                'PATCH', f'/config/apps/http/servers/{CADDY_SERVER}/listen',
                json.dumps(want).encode('utf-8'),
            )
            logger.info('configure_caddy_mode: public — srv0 listens :80+:443, auto-HTTPS on')
    else:
        if not _automatic_https_disabled():
            caddy_request(
                'PUT', f'/config/apps/http/servers/{CADDY_SERVER}/automatic_https',
                json.dumps({'disable': True}).encode('utf-8'),
            )
        a_st, a_body = caddy_request('GET', '/config/apps/tls/automation')
        if a_st == 200:
            try:
                automation = json.loads(a_body.decode('utf-8'))
            except Exception:
                automation = None
            if isinstance(automation, dict) and 'on_demand' in automation:
                caddy_request('DELETE', '/config/apps/tls/automation')
        if listen != [':80']:
            _free_listeners(servers, (':80',), CADDY_SERVER)
            caddy_request(
                'PATCH', f'/config/apps/http/servers/{CADDY_SERVER}/listen',
                json.dumps([':80']).encode('utf-8'),
            )
            logger.info('configure_caddy_mode: edge — srv0 listens :80 only, auto-HTTPS off')

    _prune_stray_routes()
    return True


def get_server_config(server: str = CADDY_SERVER) -> dict | None:
    status, body = caddy_request('GET', f'/config/apps/http/servers/{server}')
    if status != 200:
        return None
    try:
        return json.loads(body.decode('utf-8'))
    except Exception:
        return None


def list_managed_routes() -> dict[str, str]:
    status, body = caddy_request('GET', f'/config/apps/http/servers/{CADDY_SERVER}/routes')
    if status != 200:
        return {}
    try:
        data = json.loads(body.decode('utf-8'))
    except Exception:
        return {}
    out: dict[str, str] = {}
    if not isinstance(data, list):
        return out
    for r in data:
        if not isinstance(r, dict):
            continue
        rid = r.get('@id', '')
        if not ROUTE_ID_RE.match(rid):
            continue
        match = r.get('match') or []
        host = ''
        if match and isinstance(match[0], dict):
            hosts = match[0].get('host') or []
            if hosts:
                host = hosts[0]
        if not host:
            continue
        handle = r.get('handle') or []
        upstream = ''
        if handle and isinstance(handle[0], dict):
            ups = handle[0].get('upstreams') or []
            if ups and isinstance(ups[0], dict):
                upstream = ups[0].get('dial') or ''
        out[host.lower()] = upstream
    return out
