from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from steward.services import iptables
from steward.services.iptables import IptablesError


router = APIRouter()


class SshForwardRequest(BaseModel):
    host_port: int = Field(ge=22001, le=22999)
    vm_ip: str
    vm_port: int = 22


@router.post('/v1/ssh-forwards')
def add_forward(payload: SshForwardRequest) -> dict:
    try:
        iptables.add_ssh_forward(payload.host_port, payload.vm_ip, payload.vm_port)
    except IptablesError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {'ok': True, 'host_port': payload.host_port, 'vm_ip': payload.vm_ip}


@router.delete('/v1/ssh-forwards')
def remove_forward(payload: SshForwardRequest) -> dict:
    try:
        iptables.remove_ssh_forward(payload.host_port, payload.vm_ip, payload.vm_port)
    except IptablesError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {'ok': True, 'host_port': payload.host_port, 'removed': True}
