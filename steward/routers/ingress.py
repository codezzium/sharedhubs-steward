import logging
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from steward.services import ingress_caddy
from steward.services.ingress_caddy import CaddyError


logger = logging.getLogger(__name__)
router = APIRouter()

HOSTNAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.\-]{1,253}$')
DIAL_RE = re.compile(r'^[A-Za-z0-9.\-]{1,253}:\d{1,5}$')


class IngressUpsertRequest(BaseModel):
    hostname: str = Field(min_length=4, max_length=253)
    upstream_dial: str = Field(min_length=3, max_length=253)


class IngressDeleteRequest(BaseModel):
    hostname: str = Field(min_length=4, max_length=253)


@router.post('/v1/ingress/upsert')
def ingress_upsert(payload: IngressUpsertRequest) -> dict:
    if not HOSTNAME_RE.match(payload.hostname):
        raise HTTPException(status_code=400, detail='invalid hostname')
    if not DIAL_RE.match(payload.upstream_dial):
        raise HTTPException(status_code=400, detail='invalid upstream_dial (expect host:port)')

    try:
        ingress_caddy.upsert_route(payload.hostname, payload.upstream_dial)
    except CaddyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    logger.info('ingress upsert ok: %s -> %s', payload.hostname, payload.upstream_dial)
    return {'ok': True, 'hostname': payload.hostname, 'upstream': payload.upstream_dial}


@router.post('/v1/ingress/delete')
def ingress_delete(payload: IngressDeleteRequest) -> dict:
    if not HOSTNAME_RE.match(payload.hostname):
        raise HTTPException(status_code=400, detail='invalid hostname')

    try:
        removed = ingress_caddy.delete_route(payload.hostname)
    except CaddyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    logger.info('ingress delete ok: %s', payload.hostname)
    return {'ok': True, 'hostname': payload.hostname, 'removed': removed}


@router.get('/v1/ingress/list')
def ingress_list() -> dict:
    try:
        routes_map = ingress_caddy.list_managed_routes()
    except CaddyError:
        return {'routes': []}
    return {
        'routes': [
            {
                'id': ingress_caddy.route_id(hostname),
                'hostname': hostname,
                'upstream': upstream,
            }
            for hostname, upstream in routes_map.items()
        ],
    }
