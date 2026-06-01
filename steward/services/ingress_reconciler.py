import asyncio
import json
import logging
import urllib.error
import urllib.request

from steward import __version__
from steward.config import AgentConfig
from steward.services import ingress_caddy
from steward.services.ingress_caddy import CaddyError


logger = logging.getLogger(__name__)

DRIFT_CHECK_INTERVAL_SEC = 60.0
INITIAL_RETRY_DELAY_SEC = 5.0
MAX_RETRY_DELAY_SEC = 120.0

USER_AGENT = f'steward/{__version__} (+https://sharedhubs.com)'

_pending_extra: set[str] = set()


def fetch_panel_state(config: AgentConfig, timeout: float = 15.0) -> dict:
    token = config.agent_token or config.reload_token()
    if not token:
        raise RuntimeError('agent_token yok — register tamamlanmadı')
    url = f'{config.panel_url}/api/agents/ingress-state'
    req = urllib.request.Request(
        url,
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/json',
            'User-Agent': USER_AGENT,
        },
        method='GET',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'panel ingress-state HTTP {exc.code}') from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f'panel ingress-state unreachable: {exc.reason}') from exc

    return data if isinstance(data, dict) else {}


def report_drift(config: AgentConfig, missing: list[str], extra: list[str], reconciled: list[str], timeout: float = 10.0) -> None:
    token = config.agent_token or config.reload_token()
    if not token:
        return
    url = f'{config.panel_url}/api/agents/drift-event'
    body = json.dumps({
        'missing': missing,
        'extra': extra,
        'reconciled': reconciled,
    }).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'User-Agent': USER_AGENT,
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        logger.warning('drift report failed: %s', exc)


def reconcile_once(config: AgentConfig, *, report: bool = True) -> dict:
    """
    Panel'in beklediği ingress state'i Caddy'ye yansıt.

    Returns: {'missing': [...], 'extra': [...], 'reconciled': [...], 'errors': [...]}
    """
    try:
        panel_state = fetch_panel_state(config)
    except RuntimeError as exc:
        logger.warning('reconcile: panel state alınamadı: %s', exc)
        return {'missing': [], 'extra': [], 'reconciled': [], 'errors': [str(exc)]}

    panel_entries = [e for e in (panel_state.get('entries') or []) if isinstance(e, dict)]
    public_ingress = bool(panel_state.get('public_ingress'))

    try:
        ingress_caddy.configure_caddy_mode(public_ingress)
    except CaddyError as exc:
        logger.warning('reconcile: configure_caddy_mode başarısız: %s', exc)

    try:
        caddy_routes = ingress_caddy.list_managed_routes()
    except CaddyError as exc:
        logger.warning('reconcile: caddy list başarısız: %s', exc)
        return {'missing': [], 'extra': [], 'reconciled': [], 'errors': [str(exc)]}

    panel_map: dict[str, str] = {}
    for e in panel_entries:
        hostname = (e.get('hostname') or '').strip().lower()
        upstream = (e.get('upstream_dial') or '').strip()
        if hostname and upstream:
            panel_map[hostname] = upstream

    missing: list[str] = []
    reconciled: list[str] = []
    errors: list[str] = []

    for hostname, upstream in panel_map.items():
        cur = caddy_routes.get(hostname)
        if cur != upstream:
            missing.append(hostname)
            try:
                ingress_caddy.upsert_route(hostname, upstream)
                reconciled.append(hostname)
            except CaddyError as exc:
                errors.append(f'{hostname}: {exc}')

    global _pending_extra
    extra: list[str] = [h for h in caddy_routes.keys() if h not in panel_map]
    deleted: list[str] = []

    confirmed = [h for h in extra if h in _pending_extra]
    deferred = [h for h in extra if h not in _pending_extra]
    for hostname in confirmed:
        try:
            ingress_caddy.delete_route(hostname)
            deleted.append(hostname)
        except CaddyError as exc:
            errors.append(f'{hostname} (delete): {exc}')
    _pending_extra = set(extra)

    if (missing or extra) and report:
        report_drift(config, missing, extra, reconciled)

    if missing or extra:
        logger.warning(
            'ingress drift detected: missing=%d extra=%d deferred=%d deleted=%d reconciled=%d errors=%d',
            len(missing), len(extra), len(deferred), len(deleted), len(reconciled), len(errors),
        )
    else:
        logger.debug('ingress drift check ok (%d routes)', len(panel_map))

    return {
        'missing': missing,
        'extra': extra,
        'reconciled': reconciled,
        'errors': errors,
    }


async def reconcile_loop(config: AgentConfig) -> None:
    delay = INITIAL_RETRY_DELAY_SEC
    while True:
        try:
            result = await asyncio.to_thread(reconcile_once, config, report=True)
            if result['errors']:
                delay = min(delay * 2, MAX_RETRY_DELAY_SEC)
            else:
                delay = INITIAL_RETRY_DELAY_SEC
        except Exception:
            logger.exception('reconcile loop unexpected error')
            delay = min(delay * 2, MAX_RETRY_DELAY_SEC)

        await asyncio.sleep(DRIFT_CHECK_INTERVAL_SEC if delay == INITIAL_RETRY_DELAY_SEC else delay)


async def startup_reconcile(config: AgentConfig, *, max_wait_sec: float = 60.0) -> None:
    """
    Agent başladıktan hemen sonra panel'den state çekip Caddy'ye push eder.
    Agent_token henüz yoksa (ilk bootstrap, register tamamlanmamış) sessizce
    çıkar — periodic loop sonra devreye girer.
    """
    waited = 0.0
    while waited < max_wait_sec:
        if config.agent_token or config.reload_token():
            break
        await asyncio.sleep(2.0)
        waited += 2.0

    if not config.agent_token:
        logger.info('startup reconcile: agent_token henüz yok, periodic loop devralacak')
        return

    try:
        result = await asyncio.to_thread(reconcile_once, config, report=False)
        logger.info(
            'startup reconcile done: missing=%d extra=%d reconciled=%d errors=%d',
            len(result['missing']), len(result['extra']),
            len(result['reconciled']), len(result['errors']),
        )
    except Exception:
        logger.exception('startup reconcile failed')
