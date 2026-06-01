import base64
import json
import re
import shlex
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from steward.services import proxmox_local
from steward.services.proxmox_local import ProxmoxError


router = APIRouter()

GITHUB_GH_CLIENT_ID = '178c6fc778ccc68e1d6a'
GITHUB_OAUTH_SCOPES = 'repo workflow read:org'

GIT_VERSION_RE = re.compile(r'git version (\S+)')
GH_VERSION_RE = re.compile(r'gh version (\S+)')
DOCKER_VERSION_RE = re.compile(r'Docker version (\S+?),')
DOCKER_COMPOSE_VERSION_RE = re.compile(r'Docker Compose version (\S+)')
CODE_SERVER_VERSION_RE = re.compile(r'(\d+\.\d+\.\d+)')

GH_INSTALL_SCRIPT = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
type -p curl >/dev/null 2>&1 || apt-get install -y --no-install-recommends curl
mkdir -p -m 755 /etc/apt/keyrings
out=/etc/apt/keyrings/githubcli-archive-keyring.gpg
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o "$out"
chmod go+r "$out"
arch="$(dpkg --print-architecture)"
echo "deb [arch=$arch signed-by=$out] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list
apt-get update -qq
apt-get install -y --no-install-recommends gh
"""

DOCKER_INSTALL_SCRIPT = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
type -p curl >/dev/null 2>&1 || apt-get install -y --no-install-recommends curl ca-certificates
curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
sh /tmp/get-docker.sh
rm -f /tmp/get-docker.sh
systemctl enable --now docker
"""

OPENVSCODE_VERSION = '1.109.5'
OPENVSCODE_BIN = '/opt/openvscode-server/bin/openvscode-server'

EDITOR_INSTALL_SCRIPT = r"""
set -e
export HOME="${HOME:-/root}"
VER=1.109.5
DEST=/opt/openvscode-server
BIN="$DEST/bin/openvscode-server"
if [ -x "$BIN" ]; then "$BIN" --version 2>/dev/null | head -1; exit 0; fi
arch=$(uname -m)
case "$arch" in
  x86_64|amd64) A=x64;;
  aarch64|arm64) A=arm64;;
  *) echo "unsupported arch: $arch" >&2; exit 4;;
esac
export DEBIAN_FRONTEND=noninteractive
type -p curl >/dev/null 2>&1 || apt-get install -y --no-install-recommends curl ca-certificates
TMP=$(mktemp -d)
URL="https://github.com/gitpod-io/openvscode-server/releases/download/openvscode-server-v${VER}/openvscode-server-v${VER}-linux-${A}.tar.gz"
curl -fsSL "$URL" -o "$TMP/ovsc.tar.gz"
mkdir -p "$DEST"
tar -xzf "$TMP/ovsc.tar.gz" -C "$DEST" --strip-components=1
rm -rf "$TMP"
[ -x "$BIN" ] || { echo "install failed: binary missing after extract" >&2; exit 5; }
"$BIN" --version | head -1
"""


class VMProvisionRequest(BaseModel):
    # 100-199 platform/sistem VM'leri (örn. platform-data=110) için rezerve.
    vmid: int = Field(ge=200, le=8999)
    name: str
    cores: int = Field(ge=1, le=64)
    memory_mb: int = Field(ge=128)
    disk_gb: int = Field(ge=4)
    vm_ip: str
    ssh_pubkey: str


@router.post('/v1/vms')
def vm_provision(payload: VMProvisionRequest, request: Request) -> dict:
    config = request.app.state.config
    try:
        proxmox_local.vm_clone(config.template_vmid, payload.vmid, payload.name)
        proxmox_local.vm_configure(
            vmid=payload.vmid,
            cores=payload.cores,
            memory_mb=payload.memory_mb,
            bridge='vmbr1',
            vm_ip=payload.vm_ip,
            gateway='10.10.0.1',
            prefix=16,
            nameserver='1.1.1.1',
            ssh_pubkey=payload.ssh_pubkey,
        )
        proxmox_local.vm_resize_disk(payload.vmid, payload.disk_gb)
        proxmox_local.vm_start(payload.vmid)
    except ProxmoxError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {'ok': True, 'vmid': payload.vmid, 'status': 'starting'}


@router.post('/v1/vms/{vmid}/start')
def vm_start(vmid: int) -> dict:
    try:
        proxmox_local.vm_start(vmid)
    except ProxmoxError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {'ok': True, 'vmid': vmid, 'action': 'start'}


@router.post('/v1/vms/{vmid}/stop')
def vm_stop(vmid: int) -> dict:
    try:
        proxmox_local.vm_stop(vmid)
    except ProxmoxError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {'ok': True, 'vmid': vmid, 'action': 'stop'}


@router.post('/v1/vms/{vmid}/destroy')
def vm_destroy(vmid: int) -> dict:
    try:
        try:
            proxmox_local.vm_stop(vmid)
        except ProxmoxError:
            pass
        proxmox_local.vm_destroy(vmid)
    except ProxmoxError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {'ok': True, 'vmid': vmid, 'action': 'destroy'}


@router.get('/v1/vms/{vmid}/git/status')
def vm_git_status(vmid: int) -> dict:
    try:
        result = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', 'command -v git >/dev/null 2>&1 && git --version || exit 127'],
            timeout=15.0,
        )
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result['exitcode'] == 0:
        match = GIT_VERSION_RE.search(result['stdout'])
        version = match.group(1) if match else result['stdout']
        return {'installed': True, 'version': version}
    return {'installed': False, 'version': None}


@router.post('/v1/vms/{vmid}/git/install')
def vm_git_install(vmid: int) -> dict:
    try:
        update = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', 'DEBIAN_FRONTEND=noninteractive apt-get update -qq'],
            timeout=120.0,
        )
        if update['exitcode'] != 0:
            raise HTTPException(status_code=500, detail=f"apt update failed: {update['stderr'] or update['stdout']}")
        install = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', 'DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git'],
            timeout=240.0,
        )
        if install['exitcode'] != 0:
            raise HTTPException(status_code=500, detail=f"apt install git failed: {install['stderr'] or install['stdout']}")
        check = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', 'command -v git >/dev/null 2>&1 && git --version || exit 127'],
            timeout=15.0,
        )
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if check['exitcode'] != 0:
        raise HTTPException(status_code=500, detail='git installed but version check failed')
    match = GIT_VERSION_RE.search(check['stdout'])
    version = match.group(1) if match else check['stdout']
    return {'installed': True, 'version': version}


@router.get('/v1/vms/{vmid}/gh/status')
def vm_gh_status(vmid: int) -> dict:
    script = r"""
export HOME="${HOME:-/root}"
if ! command -v gh >/dev/null 2>&1; then exit 127; fi
VERSION=$(gh --version 2>/dev/null | head -1)
LOGIN=""
NAME=""
EMAIL=""
if gh auth status --hostname github.com >/dev/null 2>&1; then
  LOGIN=$(gh api user --jq .login 2>/dev/null)
  if [ -n "$LOGIN" ]; then
    NAME=$(gh api user --jq '.name // .login' 2>/dev/null)
    EMAIL=$(gh api user --jq '.email // empty' 2>/dev/null)
  fi
fi
printf 'VERSION=%s\n' "$VERSION"
printf 'LOGIN=%s\n' "$LOGIN"
printf 'NAME_B64=%s\n' "$(printf '%s' "$NAME" | base64 -w0)"
printf 'EMAIL=%s\n' "$EMAIL"
"""
    try:
        result = proxmox_local.vm_guest_exec(vmid, ['bash', '-lc', script], timeout=25.0)
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result['exitcode'] == 127:
        return {'installed': False, 'version': None, 'login': '', 'name': '', 'email': ''}
    if result['exitcode'] != 0:
        raise HTTPException(status_code=500, detail=result['stderr'] or result['stdout'] or 'gh status failed')

    out: dict[str, str] = {}
    for line in result['stdout'].splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            out[k] = v.strip()
    version_raw = out.get('VERSION', '')
    match = GH_VERSION_RE.search(version_raw)
    name_b64 = out.get('NAME_B64', '')
    try:
        name = base64.b64decode(name_b64).decode('utf-8') if name_b64 else ''
    except (ValueError, UnicodeDecodeError):
        name = ''
    return {
        'installed': True,
        'version': match.group(1) if match else version_raw,
        'login': out.get('LOGIN', ''),
        'name': name,
        'email': out.get('EMAIL', ''),
    }


@router.post('/v1/vms/{vmid}/gh/install')
def vm_gh_install(vmid: int) -> dict:
    try:
        install = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', GH_INSTALL_SCRIPT],
            timeout=300.0,
        )
        if install['exitcode'] != 0:
            raise HTTPException(status_code=500, detail=f"gh install failed: {install['stderr'] or install['stdout']}")
        check = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', 'command -v gh >/dev/null 2>&1 && gh --version | head -1 || exit 127'],
            timeout=15.0,
        )
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if check['exitcode'] != 0:
        raise HTTPException(status_code=500, detail='gh installed but version check failed')
    match = GH_VERSION_RE.search(check['stdout'])
    version = match.group(1) if match else check['stdout']
    return {'installed': True, 'version': version}


@router.get('/v1/vms/{vmid}/docker/status')
def vm_docker_status(vmid: int) -> dict:
    try:
        docker_check = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', 'command -v docker >/dev/null 2>&1 && docker --version || exit 127'],
            timeout=15.0,
        )
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if docker_check['exitcode'] != 0:
        return {'installed': False, 'version': None, 'compose_version': None}

    match = DOCKER_VERSION_RE.search(docker_check['stdout'])
    version = match.group(1) if match else docker_check['stdout']

    compose_version = ''
    try:
        compose_check = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', 'docker compose version 2>/dev/null || exit 127'],
            timeout=15.0,
        )
        if compose_check['exitcode'] == 0:
            cm = DOCKER_COMPOSE_VERSION_RE.search(compose_check['stdout'])
            compose_version = cm.group(1) if cm else compose_check['stdout'].strip()
    except ProxmoxError:
        pass

    return {'installed': True, 'version': version, 'compose_version': compose_version}


@router.post('/v1/vms/{vmid}/docker/install')
def vm_docker_install(vmid: int) -> dict:
    try:
        install = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', DOCKER_INSTALL_SCRIPT],
            timeout=420.0,
        )
        if install['exitcode'] != 0:
            raise HTTPException(
                status_code=500,
                detail=f"docker install failed: {install['stderr'] or install['stdout']}",
            )
        check = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', 'command -v docker >/dev/null 2>&1 && docker --version || exit 127'],
            timeout=15.0,
        )
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if check['exitcode'] != 0:
        raise HTTPException(status_code=500, detail='docker installed but version check failed')
    vm = DOCKER_VERSION_RE.search(check['stdout'])
    version = vm.group(1) if vm else check['stdout']

    compose_version = ''
    try:
        compose_check = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', 'docker compose version 2>/dev/null || exit 127'],
            timeout=15.0,
        )
        if compose_check['exitcode'] == 0:
            cm = DOCKER_COMPOSE_VERSION_RE.search(compose_check['stdout'])
            compose_version = cm.group(1) if cm else compose_check['stdout'].strip()
    except ProxmoxError:
        pass

    return {'installed': True, 'version': version, 'compose_version': compose_version}


class EditorConfigureRequest(BaseModel):
    port: int = Field(ge=1024, le=65535)
    token: str = Field(min_length=8, max_length=200)


@router.get('/v1/vms/{vmid}/editor/status')
def vm_editor_status(vmid: int) -> dict:
    script = r"""
BIN=/opt/openvscode-server/bin/openvscode-server
if [ ! -x "$BIN" ]; then exit 127; fi
VERSION=$("$BIN" --version 2>/dev/null | head -1)
ACTIVE=$(systemctl is-active openvscode-server 2>/dev/null || true)
printf 'VERSION=%s\n' "$VERSION"
printf 'ACTIVE=%s\n' "$ACTIVE"
"""
    try:
        result = proxmox_local.vm_guest_exec(vmid, ['bash', '-lc', script], timeout=20.0)
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result['exitcode'] == 127:
        return {'installed': False, 'running': False, 'version': None}
    if result['exitcode'] != 0:
        raise HTTPException(status_code=500, detail=result['stderr'] or result['stdout'] or 'editor status failed')
    out: dict[str, str] = {}
    for line in result['stdout'].splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            out[k] = v.strip()
    match = CODE_SERVER_VERSION_RE.search(out.get('VERSION', ''))
    return {
        'installed': True,
        'running': out.get('ACTIVE', '') == 'active',
        'version': match.group(1) if match else out.get('VERSION', ''),
    }


@router.post('/v1/vms/{vmid}/editor/install')
def vm_editor_install(vmid: int) -> dict:
    try:
        install = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', EDITOR_INSTALL_SCRIPT],
            timeout=420.0,
        )
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if install['exitcode'] != 0:
        raise HTTPException(
            status_code=500,
            detail=f"openvscode-server install failed: {install['stderr'] or install['stdout']}",
        )
    match = CODE_SERVER_VERSION_RE.search(install['stdout'])
    return {'installed': True, 'version': match.group(1) if match else install['stdout'].strip()}


@router.post('/v1/vms/{vmid}/editor/configure')
def vm_editor_configure(vmid: int, payload: EditorConfigureRequest) -> dict:
    quoted_token = shlex.quote(payload.token)
    script = f"""
set -e
export HOME="${{HOME:-/root}}"
BIN=/opt/openvscode-server/bin/openvscode-server
if [ ! -x "$BIN" ]; then echo "openvscode-server binary not found" >&2; exit 3; fi
systemctl disable --now code-server >/dev/null 2>&1 || true
mkdir -p /root/.config/openvscode-server
umask 077
printf '%s' {quoted_token} > /root/.config/openvscode-server/token
chmod 600 /root/.config/openvscode-server/token
cat > /etc/systemd/system/openvscode-server.service <<UNIT
[Unit]
Description=openvscode-server (SharedHub)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=HOME=/root
ExecStart=/opt/openvscode-server/bin/openvscode-server --host 0.0.0.0 --port {payload.port} --connection-token-file /root/.config/openvscode-server/token
Restart=always
User=root

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable openvscode-server >/dev/null 2>&1 || true
systemctl restart openvscode-server
sleep 2
ACTIVE=$(systemctl is-active openvscode-server 2>/dev/null || true)
printf 'ACTIVE=%s\n' "$ACTIVE"
"""
    try:
        result = proxmox_local.vm_guest_exec(vmid, ['bash', '-lc', script], timeout=60.0)
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result['exitcode'] != 0:
        raise HTTPException(
            status_code=500,
            detail=f"editor configure failed: {(result['stderr'] or result['stdout'])[:400]}",
        )
    return {'ok': True, 'running': 'ACTIVE=active' in result['stdout'], 'port': payload.port}


@router.post('/v1/vms/{vmid}/editor/stop')
def vm_editor_stop(vmid: int) -> dict:
    script = r"""
systemctl disable --now openvscode-server >/dev/null 2>&1 || true
systemctl is-active openvscode-server 2>/dev/null || true
"""
    try:
        result = proxmox_local.vm_guest_exec(vmid, ['bash', '-lc', script], timeout=30.0)
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {'ok': True, 'running': result['stdout'].strip() == 'active'}


def _github_post(url: str, payload: dict, timeout: float = 10.0) -> dict:
    data = urllib.parse.urlencode(payload).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8')
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f'GitHub request failed: {exc}') from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail=f'GitHub returned non-JSON: {body[:200]}') from exc


class GhAuthPollRequest(BaseModel):
    device_code: str


@router.post('/v1/vms/{vmid}/gh/auth/start')
def vm_gh_auth_start(vmid: int) -> dict:
    try:
        check = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', 'command -v gh >/dev/null 2>&1 || exit 127'],
            timeout=10.0,
        )
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if check['exitcode'] != 0:
        raise HTTPException(status_code=400, detail='gh CLI is not installed in this VM')

    data = _github_post(
        'https://github.com/login/device/code',
        {'client_id': GITHUB_GH_CLIENT_ID, 'scope': GITHUB_OAUTH_SCOPES},
    )
    if 'device_code' not in data:
        raise HTTPException(status_code=502, detail=f'GitHub device code error: {data}')
    return {
        'device_code': data['device_code'],
        'user_code': data['user_code'],
        'verification_uri': data.get('verification_uri', 'https://github.com/login/device'),
        'expires_in': int(data.get('expires_in', 900)),
        'interval': int(data.get('interval', 5)),
    }


@router.post('/v1/vms/{vmid}/gh/auth/poll')
def vm_gh_auth_poll(vmid: int, payload: GhAuthPollRequest) -> dict:
    data = _github_post(
        'https://github.com/login/oauth/access_token',
        {
            'client_id': GITHUB_GH_CLIENT_ID,
            'device_code': payload.device_code,
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
        },
    )
    err = data.get('error')
    if err in ('authorization_pending', 'slow_down'):
        return {'status': 'pending'}
    if err == 'expired_token':
        return {'status': 'expired'}
    if err == 'access_denied':
        return {'status': 'denied'}
    if err:
        return {'status': 'error', 'error': data.get('error_description') or err}

    token = data.get('access_token')
    if not token:
        return {'status': 'error', 'error': 'no access_token in response'}

    save_script = r"""
set -e
export HOME="${HOME:-/root}"
cd "$HOME"
echo "$1" | gh auth login --with-token --hostname github.com
gh auth setup-git --hostname github.com
LOGIN=$(gh api user --jq .login)
NAME=$(gh api user --jq '.name // .login')
EMAIL=$(gh api user --jq '.email // empty')
if [ -z "$EMAIL" ]; then
    EMAIL="${LOGIN}@users.noreply.github.com"
fi
git config --global user.name "$NAME"
git config --global user.email "$EMAIL"
printf '{"login":"%s","name":"%s","email":"%s"}' "$LOGIN" "$NAME" "$EMAIL"
"""
    try:
        result = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-c', save_script, '_', token],
            timeout=30.0,
        )
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result['exitcode'] != 0:
        raise HTTPException(
            status_code=500,
            detail=f"failed to save token in VM: {result['stderr'] or result['stdout']}",
        )

    last_line = result['stdout'].splitlines()[-1] if result['stdout'] else ''
    try:
        user_info = json.loads(last_line)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f'invalid user info JSON: {last_line!r}') from exc

    return {
        'status': 'completed',
        'login': user_info.get('login', ''),
        'name': user_info.get('name', ''),
        'email': user_info.get('email', ''),
    }


def _gh_exec_json(vmid: int, gh_args: list[str], timeout: float = 60.0):
    cmd = shlex.join(['gh', *gh_args])
    try:
        result = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', f'export HOME="${{HOME:-/root}}"; {cmd}'],
            timeout=timeout,
        )
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result['exitcode'] != 0:
        detail = (result['stderr'] or result['stdout'] or 'gh api failed').strip()
        if 'authentication' in detail.lower() or 'not logged into' in detail.lower():
            raise HTTPException(status_code=401, detail='gh CLI is not authenticated')
        raise HTTPException(status_code=502, detail=f'gh api failed: {detail[:400]}')
    return result['stdout']


def _gh_lines(vmid: int, gh_args: list[str], timeout: float = 60.0) -> list[dict]:
    stdout = _gh_exec_json(vmid, gh_args, timeout=timeout)
    out: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _gh_one(vmid: int, gh_args: list[str], timeout: float = 30.0) -> dict:
    stdout = _gh_exec_json(vmid, gh_args, timeout=timeout)
    stdout = stdout.strip()
    if not stdout:
        return {}
    return json.loads(stdout)


@router.get('/v1/vms/{vmid}/gh/orgs')
def vm_gh_orgs(vmid: int, q: str = Query(default='')) -> dict:
    user = _gh_one(
        vmid,
        ['api', 'user', '--jq', '{login, name, avatar_url}'],
        timeout=20.0,
    )
    if not user.get('login'):
        raise HTTPException(status_code=401, detail='gh CLI is not authenticated')

    orgs = _gh_lines(
        vmid,
        ['api', '--paginate', 'user/orgs?per_page=100',
         '--jq', '.[] | {login, name: (.description // .login), avatar_url}'],
        timeout=30.0,
    )

    owners = [
        {
            'login': user['login'],
            'name': user.get('name') or user['login'],
            'avatar_url': user.get('avatar_url') or '',
            'type': 'user',
        },
    ]
    for o in orgs:
        owners.append({
            'login': o.get('login', ''),
            'name': o.get('name') or o.get('login', ''),
            'avatar_url': o.get('avatar_url') or '',
            'type': 'org',
        })

    if q:
        ql = q.lower()
        owners = [o for o in owners if ql in o['login'].lower() or ql in o['name'].lower()]

    return {'owners': owners, 'me': user['login']}


@router.get('/v1/vms/{vmid}/gh/repos')
def vm_gh_repos(
    vmid: int,
    owner: str = Query(...),
    owner_type: str = Query(default='org'),
    q: str = Query(default=''),
) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', owner):
        raise HTTPException(status_code=400, detail='invalid owner')

    jq = '.[] | {name, description, default_branch, private, fork, archived, html_url, updated_at, pushed_at}'
    if owner_type == 'user':
        me_login = _gh_exec_json(vmid, ['api', 'user', '--jq', '.login'], timeout=15.0).strip()
        if me_login == owner:
            repos = _gh_lines(
                vmid,
                ['api', '--paginate',
                 'user/repos?per_page=100&affiliation=owner&sort=updated',
                 '--jq', jq],
                timeout=60.0,
            )
        else:
            repos = _gh_lines(
                vmid,
                ['api', '--paginate',
                 f'users/{owner}/repos?per_page=100&sort=updated',
                 '--jq', jq],
                timeout=60.0,
            )
    else:
        repos = _gh_lines(
            vmid,
            ['api', '--paginate',
             f'orgs/{owner}/repos?per_page=100&sort=updated',
             '--jq', jq],
            timeout=60.0,
        )

    if q:
        ql = q.lower()
        repos = [r for r in repos if ql in (r.get('name') or '').lower()
                 or ql in (r.get('description') or '').lower()]

    return {'repos': repos[:200]}


@router.get('/v1/vms/{vmid}/gh/branches')
def vm_gh_branches(
    vmid: int,
    owner: str = Query(...),
    repo: str = Query(...),
    q: str = Query(default=''),
) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', owner):
        raise HTTPException(status_code=400, detail='invalid owner')
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', repo):
        raise HTTPException(status_code=400, detail='invalid repo')

    info = _gh_one(
        vmid,
        ['api', f'repos/{owner}/{repo}', '--jq', '{default_branch, html_url, private}'],
        timeout=20.0,
    )

    branches = _gh_lines(
        vmid,
        ['api', '--paginate',
         f'repos/{owner}/{repo}/branches?per_page=100',
         '--jq', '.[] | {name, protected, sha: .commit.sha}'],
        timeout=60.0,
    )

    if q:
        ql = q.lower()
        branches = [b for b in branches if ql in (b.get('name') or '').lower()]

    return {
        'branches': branches[:300],
        'default_branch': info.get('default_branch', 'main'),
        'html_url': info.get('html_url', ''),
        'private': bool(info.get('private', False)),
    }


@router.post('/v1/vms/{vmid}/gh/auth/logout')
def vm_gh_auth_logout(vmid: int) -> dict:
    script = """
export HOME="${HOME:-/root}"
cd "$HOME"
gh auth logout --hostname github.com -y 2>/dev/null || true
git config --global --unset user.name 2>/dev/null || true
git config --global --unset user.email 2>/dev/null || true
"""
    try:
        proxmox_local.vm_guest_exec(vmid, ['bash', '-lc', script], timeout=15.0)
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {'ok': True}


PATH_RE = re.compile(r'^/[A-Za-z0-9._/\-]{1,255}$')
SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,62}$')


class ProjectCloneRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=63)
    owner: str = Field(min_length=1, max_length=120)
    repo: str = Field(min_length=1, max_length=120)
    branch: str = Field(min_length=1, max_length=255)
    deploy_path: str = Field(min_length=1, max_length=255)


@router.get('/v1/vms/{vmid}/projects/discover')
def vm_projects_discover(vmid: int) -> dict:
    script = r"""
set -e
ROOT=/srv/projects
[ -d "$ROOT" ] || exit 0
for d in "$ROOT"/*/; do
  [ -d "$d" ] || continue
  name=$(basename "$d")
  compose=""
  for f in docker-compose.yml docker-compose.yaml compose.yml compose.yaml; do
    [ -f "$d$f" ] && compose="$compose$f,"
  done
  remote=""
  branch=""
  if [ -d "$d.git" ]; then
    remote=$(git -C "$d" remote get-url origin 2>/dev/null || true)
    branch=$(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
  fi
  printf 'PROJECT|%s|%s|%s|%s\n' "$name" "$compose" "$remote" "$branch"
done
"""
    try:
        result = proxmox_local.vm_guest_exec(vmid, ['bash', '-lc', script], timeout=30.0)
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if result['exitcode'] != 0:
        raise HTTPException(status_code=500, detail=result.get('stderr') or 'discover failed')

    projects = []
    for line in result['stdout'].splitlines():
        if not line.startswith('PROJECT|'):
            continue
        fields = (line.split('|') + ['', '', '', ''])[:5]
        name, compose, remote, branch = fields[1], fields[2], fields[3], fields[4]
        projects.append({
            'name': name,
            'deploy_path': f'/srv/projects/{name}',
            'compose_files': [c for c in compose.split(',') if c],
            'git_remote': remote,
            'git_branch': branch,
        })
    return {'projects': projects}


@router.post('/v1/vms/{vmid}/projects/clone')
def vm_project_clone(vmid: int, payload: ProjectCloneRequest) -> dict:
    if not SLUG_RE.match(payload.slug):
        raise HTTPException(status_code=400, detail='invalid slug')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', payload.owner):
        raise HTTPException(status_code=400, detail='invalid owner')
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', payload.repo):
        raise HTTPException(status_code=400, detail='invalid repo')
    if not re.fullmatch(r'[A-Za-z0-9._/\-]{1,255}', payload.branch):
        raise HTTPException(status_code=400, detail='invalid branch')
    if not PATH_RE.match(payload.deploy_path):
        raise HTTPException(status_code=400, detail='invalid deploy_path')

    parent = payload.deploy_path.rsplit('/', 1)[0] or '/'
    quoted_parent = shlex.quote(parent)
    quoted_target = shlex.quote(payload.deploy_path)
    quoted_owner = shlex.quote(payload.owner)
    quoted_repo = shlex.quote(payload.repo)
    quoted_branch = shlex.quote(payload.branch)

    script = f"""
set -e
export HOME="${{HOME:-/root}}"
mkdir -p {quoted_parent}
if [ -d {quoted_target}/.git ]; then
  cd {quoted_target}
  git fetch --prune origin
  git checkout {quoted_branch}
  git reset --hard origin/{quoted_branch}
  ACTION=updated
else
  rm -rf {quoted_target}
  gh repo clone {quoted_owner}/{quoted_repo} {quoted_target} -- --branch {quoted_branch}
  ACTION=cloned
fi
cd {quoted_target}
COMMIT=$(git rev-parse HEAD)
echo "ACTION=$ACTION"
echo "COMMIT=$COMMIT"
"""

    try:
        result = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', script],
            timeout=300.0,
        )
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    stdout = result.get('stdout', '')
    stderr = result.get('stderr', '')
    log = (stdout + ('\n' + stderr if stderr else '')).strip()

    if result['exitcode'] != 0:
        return {
            'ok': False,
            'error': 'clone failed',
            'log': log[:4000],
            'deploy_path': payload.deploy_path,
        }

    action = ''
    commit = ''
    for line in stdout.splitlines():
        if line.startswith('ACTION='):
            action = line.split('=', 1)[1].strip()
        elif line.startswith('COMMIT='):
            commit = line.split('=', 1)[1].strip()

    return {
        'ok': True,
        'action': action or 'cloned',
        'commit': commit,
        'deploy_path': payload.deploy_path,
        'log': log[:4000],
    }


ENV_MAX_BYTES = 65536


class ProjectEnvWriteRequest(BaseModel):
    deploy_path: str = Field(min_length=1, max_length=255)
    content: str = Field(max_length=ENV_MAX_BYTES * 2)


@router.get('/v1/vms/{vmid}/projects/env')
def vm_project_env_read(vmid: int, deploy_path: str = Query(...)) -> dict:
    if not PATH_RE.match(deploy_path):
        raise HTTPException(status_code=400, detail='invalid deploy_path')

    quoted = shlex.quote(deploy_path)
    script = f"""
set -e
DIR={quoted}
if [ ! -d "$DIR" ]; then
  echo "===STATUS==="
  echo "no_dir"
  exit 0
fi

ENV_EXAMPLE_EXISTS=0
ENV_EXAMPLE_B64=""
if [ -f "$DIR/.env.example" ]; then
  ENV_EXAMPLE_EXISTS=1
  ENV_EXAMPLE_B64=$(base64 -w0 < "$DIR/.env.example" || true)
fi

ENV_EXISTS=0
ENV_B64=""
ENV_SIZE=0
ENV_MTIME=0
if [ -f "$DIR/.env" ]; then
  ENV_EXISTS=1
  ENV_B64=$(base64 -w0 < "$DIR/.env" || true)
  ENV_SIZE=$(stat -c "%s" "$DIR/.env" 2>/dev/null || echo 0)
  ENV_MTIME=$(stat -c "%Y" "$DIR/.env" 2>/dev/null || echo 0)
fi

echo "===STATUS==="
echo "ok"
echo "===EXAMPLE_EXISTS==="
echo "$ENV_EXAMPLE_EXISTS"
echo "===EXAMPLE==="
echo "$ENV_EXAMPLE_B64"
echo "===ENV_EXISTS==="
echo "$ENV_EXISTS"
echo "===ENV_SIZE==="
echo "$ENV_SIZE"
echo "===ENV_MTIME==="
echo "$ENV_MTIME"
echo "===ENV==="
echo "$ENV_B64"
"""

    try:
        result = proxmox_local.vm_guest_exec(vmid, ['bash', '-lc', script], timeout=20.0)
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result['exitcode'] != 0:
        raise HTTPException(
            status_code=502,
            detail=f"env read failed: {(result['stderr'] or result['stdout'])[:400]}",
        )

    sections: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in result['stdout'].splitlines():
        if line.startswith('===') and line.endswith('==='):
            if current is not None:
                sections[current] = '\n'.join(buf).strip()
            current = line.strip('=')
            buf = []
        else:
            buf.append(line)
    if current is not None:
        sections[current] = '\n'.join(buf).strip()

    if sections.get('STATUS') == 'no_dir':
        raise HTTPException(status_code=404, detail='deploy_path does not exist on VM')

    def _decode(b64: str) -> str:
        if not b64:
            return ''
        try:
            return base64.b64decode(b64).decode('utf-8', errors='replace')
        except Exception:
            return ''

    return {
        'has_example': sections.get('EXAMPLE_EXISTS') == '1',
        'example': _decode(sections.get('EXAMPLE', '')),
        'has_env': sections.get('ENV_EXISTS') == '1',
        'env': _decode(sections.get('ENV', '')),
        'env_size': int(sections.get('ENV_SIZE') or 0),
        'env_mtime': int(sections.get('ENV_MTIME') or 0),
    }


@router.post('/v1/vms/{vmid}/projects/env')
def vm_project_env_write(vmid: int, payload: ProjectEnvWriteRequest) -> dict:
    if not PATH_RE.match(payload.deploy_path):
        raise HTTPException(status_code=400, detail='invalid deploy_path')
    if len(payload.content.encode('utf-8')) > ENV_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f'.env exceeds {ENV_MAX_BYTES} bytes')

    encoded = base64.b64encode(payload.content.encode('utf-8')).decode('ascii')
    quoted_dir = shlex.quote(payload.deploy_path)
    quoted_b64 = shlex.quote(encoded)

    script = f"""
set -e
DIR={quoted_dir}
if [ ! -d "$DIR" ]; then
  echo "no_dir" >&2
  exit 9
fi
TMP="$DIR/.env.tmp.$$"
umask 077
printf '%s' {quoted_b64} | base64 -d > "$TMP"
chmod 600 "$TMP"
mv "$TMP" "$DIR/.env"
SIZE=$(stat -c "%s" "$DIR/.env")
MTIME=$(stat -c "%Y" "$DIR/.env")
echo "SIZE=$SIZE"
echo "MTIME=$MTIME"
"""

    try:
        result = proxmox_local.vm_guest_exec(vmid, ['bash', '-lc', script], timeout=20.0)
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result['exitcode'] == 9:
        raise HTTPException(status_code=404, detail='deploy_path does not exist on VM')
    if result['exitcode'] != 0:
        raise HTTPException(
            status_code=500,
            detail=f"env write failed: {(result['stderr'] or result['stdout'])[:400]}",
        )

    size = 0
    mtime = 0
    for line in result['stdout'].splitlines():
        if line.startswith('SIZE='):
            try:
                size = int(line.split('=', 1)[1])
            except ValueError:
                pass
        elif line.startswith('MTIME='):
            try:
                mtime = int(line.split('=', 1)[1])
            except ValueError:
                pass

    return {'ok': True, 'env_size': size, 'env_mtime': mtime}


COMPOSE_FILE_RE = re.compile(r'^(docker-)?compose([.\-][A-Za-z0-9._\-]+)?\.ya?ml$')


class ProjectComposeRequest(BaseModel):
    deploy_path: str = Field(min_length=1, max_length=255)
    compose_file: str = Field(min_length=1, max_length=128)


def _validate_compose_args(deploy_path: str, compose_file: str) -> None:
    if not PATH_RE.match(deploy_path):
        raise HTTPException(status_code=400, detail='invalid deploy_path')
    if not COMPOSE_FILE_RE.match(compose_file):
        raise HTTPException(status_code=400, detail='invalid compose_file name')


@router.get('/v1/vms/{vmid}/projects/compose/files')
def vm_project_compose_files(vmid: int, deploy_path: str = Query(...)) -> dict:
    if not PATH_RE.match(deploy_path):
        raise HTTPException(status_code=400, detail='invalid deploy_path')
    quoted = shlex.quote(deploy_path)
    script = f"""
set -e
DIR={quoted}
if [ ! -d "$DIR" ]; then
  echo "no_dir"
  exit 0
fi
cd "$DIR"
ls -1 2>/dev/null | grep -iE '^(docker-)?compose(\\.[A-Za-z0-9._-]+)?\\.ya?ml$' || true
"""
    try:
        result = proxmox_local.vm_guest_exec(vmid, ['bash', '-lc', script], timeout=15.0)
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result['exitcode'] != 0:
        raise HTTPException(
            status_code=502,
            detail=f"compose files listing failed: {(result['stderr'] or result['stdout'])[:400]}",
        )

    stdout = result['stdout'].strip()
    if stdout == 'no_dir':
        raise HTTPException(status_code=404, detail='deploy_path does not exist on VM')

    files = [
        line.strip() for line in stdout.splitlines()
        if line.strip() and COMPOSE_FILE_RE.match(line.strip())
    ]
    return {'files': files}


@router.post('/v1/vms/{vmid}/projects/compose/up')
def vm_project_compose_up(vmid: int, payload: ProjectComposeRequest) -> dict:
    _validate_compose_args(payload.deploy_path, payload.compose_file)
    quoted_dir = shlex.quote(payload.deploy_path)
    quoted_file = shlex.quote(payload.compose_file)
    script = f"""
set -e
cd {quoted_dir}
if [ ! -f {quoted_file} ]; then
  echo "no_file" >&2
  exit 9
fi
docker compose -f {quoted_file} up -d --build --remove-orphans 2>&1
echo "===EXIT==="
echo "$?"
"""
    try:
        result = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', script],
            timeout=1200.0,
        )
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    stdout = result.get('stdout', '')
    stderr = result.get('stderr', '')
    log = (stdout + ('\n' + stderr if stderr else '')).strip()

    LOG_CAP = 200_000
    tail = log[-LOG_CAP:] if len(log) > LOG_CAP else log
    if result['exitcode'] == 9:
        return {'ok': False, 'error': f'compose file "{payload.compose_file}" not found', 'log': tail}
    if result['exitcode'] != 0:
        return {'ok': False, 'error': 'compose up failed', 'log': tail}
    return {'ok': True, 'log': tail}


@router.post('/v1/vms/{vmid}/projects/compose/down')
def vm_project_compose_down(vmid: int, payload: ProjectComposeRequest) -> dict:
    _validate_compose_args(payload.deploy_path, payload.compose_file)
    quoted_dir = shlex.quote(payload.deploy_path)
    quoted_file = shlex.quote(payload.compose_file)
    script = f"""
set -e
cd {quoted_dir}
if [ ! -f {quoted_file} ]; then
  echo "no_file" >&2
  exit 9
fi
docker compose -f {quoted_file} down --remove-orphans 2>&1
"""
    try:
        result = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', script],
            timeout=300.0,
        )
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    stdout = result.get('stdout', '')
    stderr = result.get('stderr', '')
    log = (stdout + ('\n' + stderr if stderr else '')).strip()

    LOG_CAP = 200_000
    tail = log[-LOG_CAP:] if len(log) > LOG_CAP else log
    if result['exitcode'] == 9:
        return {'ok': False, 'error': f'compose file "{payload.compose_file}" not found', 'log': tail}
    if result['exitcode'] != 0:
        return {'ok': False, 'error': 'compose down failed', 'log': tail}
    return {'ok': True, 'log': tail}


@router.get('/v1/vms/{vmid}/projects/compose/ps')
def vm_project_compose_ps(
    vmid: int,
    deploy_path: str = Query(...),
    compose_file: str = Query(...),
) -> dict:
    _validate_compose_args(deploy_path, compose_file)
    quoted_dir = shlex.quote(deploy_path)
    quoted_file = shlex.quote(compose_file)
    script = f"""
set -e
cd {quoted_dir}
if [ ! -f {quoted_file} ]; then
  echo "no_file" >&2
  exit 9
fi
docker compose -f {quoted_file} ps --format json 2>/dev/null || true
"""
    try:
        result = proxmox_local.vm_guest_exec(vmid, ['bash', '-lc', script], timeout=20.0)
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if result['exitcode'] == 9:
        raise HTTPException(status_code=404, detail=f'compose file "{compose_file}" not found')
    if result['exitcode'] != 0:
        raise HTTPException(
            status_code=502,
            detail=f"compose ps failed: {(result['stderr'] or result['stdout'])[:400]}",
        )

    containers: list[dict] = []
    stdout = result.get('stdout', '').strip()
    if stdout:
        if stdout.startswith('['):
            try:
                parsed = json.loads(stdout)
                if isinstance(parsed, list):
                    containers = parsed
            except json.JSONDecodeError:
                pass
        else:
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    containers.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    simplified = []
    for c in containers:
        simplified.append({
            'name': c.get('Name') or c.get('Names') or '',
            'service': c.get('Service') or '',
            'state': c.get('State') or '',
            'status': c.get('Status') or '',
            'image': c.get('Image') or '',
            'ports': c.get('Publishers') or c.get('Ports') or '',
        })
    return {'containers': simplified}


SERVICE_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._\-]{0,62}$')


@router.get('/v1/vms/{vmid}/projects/compose/services')
def vm_project_compose_services(
    vmid: int,
    deploy_path: str = Query(...),
    compose_file: str = Query(...),
) -> dict:
    _validate_compose_args(deploy_path, compose_file)
    quoted_dir = shlex.quote(deploy_path)
    quoted_file = shlex.quote(compose_file)
    script = f"""
set -e
cd {quoted_dir}
if [ ! -f {quoted_file} ]; then
  echo "no_file" >&2
  exit 9
fi
docker compose -f {quoted_file} config --format json 2>&1
"""
    try:
        result = proxmox_local.vm_guest_exec(vmid, ['bash', '-lc', script], timeout=30.0)
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if result['exitcode'] == 9:
        raise HTTPException(status_code=404, detail=f'compose file "{compose_file}" not found')
    if result['exitcode'] != 0:
        raise HTTPException(
            status_code=502,
            detail=f"compose config failed: {(result['stderr'] or result['stdout'])[:400]}",
        )

    try:
        config = json.loads(result['stdout'])
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail=f'invalid compose config JSON: {exc}') from exc

    services_raw = config.get('services') or {}
    services: list[dict] = []
    for name, svc in services_raw.items():
        ports = svc.get('ports') or []
        published_ports: list[dict] = []
        for p in ports:
            if not isinstance(p, dict):
                continue
            published = p.get('published')
            target = p.get('target')
            protocol = p.get('protocol') or 'tcp'
            if not published or protocol != 'tcp':
                continue
            try:
                pub = int(published)
            except (TypeError, ValueError):
                continue
            published_ports.append({
                'published': pub,
                'target': int(target) if target else pub,
                'protocol': protocol,
            })
        services.append({
            'name': name,
            'image': svc.get('image') or '',
            'ports': published_ports,
        })
    return {'services': services}


@router.get('/v1/vms/{vmid}/projects/compose/logs')
def vm_project_compose_logs(
    vmid: int,
    deploy_path: str = Query(...),
    compose_file: str = Query(...),
    service: str = Query(...),
    tail: int = Query(default=300, ge=10, le=10000),
) -> dict:
    _validate_compose_args(deploy_path, compose_file)
    if not SERVICE_NAME_RE.match(service):
        raise HTTPException(status_code=400, detail='invalid service name')

    quoted_dir = shlex.quote(deploy_path)
    quoted_file = shlex.quote(compose_file)
    quoted_service = shlex.quote(service)

    script = f"""
set -e
cd {quoted_dir}
if [ ! -f {quoted_file} ]; then
  echo "no_file" >&2
  exit 9
fi
docker compose -f {quoted_file} logs --no-color --no-log-prefix --tail {tail} {quoted_service} 2>&1
"""
    try:
        result = proxmox_local.vm_guest_exec(
            vmid,
            ['bash', '-lc', script],
            timeout=30.0,
        )
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if result['exitcode'] == 9:
        raise HTTPException(status_code=404, detail=f'compose file "{compose_file}" not found')
    if result['exitcode'] != 0:
        raise HTTPException(
            status_code=502,
            detail=f"compose logs failed: {(result['stderr'] or result['stdout'])[:400]}",
        )

    logs = result.get('stdout') or ''
    LOG_CAP = 200_000
    if len(logs) > LOG_CAP:
        logs = logs[-LOG_CAP:]
    return {'service': service, 'tail': tail, 'logs': logs}


@router.get('/v1/vms/{vmid}/status')
def vm_status(vmid: int) -> dict:
    try:
        status = proxmox_local.vm_status(vmid)
    except ProxmoxError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    metrics = proxmox_local.vm_resources(vmid) if status == 'running' else {}
    return {'vmid': vmid, 'status': status, 'metrics': metrics}
