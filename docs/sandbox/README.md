# Sandbox Provider Plugin

SWE-Squad uses a pluggable sandbox system so developer agents can test fixes
in an isolated environment before opening a pull request.

The sandbox backend is **fully swappable** — choose the one that fits your
infrastructure. No core code changes required.

## Available Providers

| Provider | Best for | Requires |
|---|---|---|
| `proxmox` | Teams with a Proxmox cluster | ProxmoxAI gateway + API key |
| `docker` | Local Docker installs | Docker daemon |
| `local` | No infra (unit tests only) | Nothing |

## Configuration

Set the active provider in `config/swe_team.yaml`:

```yaml
providers:
  sandbox:
    provider: proxmox       # proxmox | docker | local

    # ProxmoxAI settings (ignored if provider != proxmox)
    gateway_url: ""         # override with PROXMOXAI_GATEWAY_URL env var
    api_key: ""             # override with PROXMOXAI_API_KEY env var
    node: io                # Proxmox node name
    default_cpu: 2
    default_ram_gb: 4
    default_disk_gb: 20
    default_ttl_hours: 2
```

**All secrets go in `.env`, never in yaml:**

```env
PROXMOXAI_GATEWAY_URL=http://<your-gateway>:8080
PROXMOXAI_API_KEY=<your-api-key>
```

## ProxmoxAI Provider

The `proxmox` provider connects to a [ProxmoxAI](https://github.com/ArtemisAI/proxmoxAI)
gateway — a REST API layer over Proxmox VE that handles VM lifecycle with
quota enforcement and multi-tenant isolation.

### Prerequisites

1. A running Proxmox VE cluster
2. ProxmoxAI gateway deployed (see the proxmoxAI repo)
3. An API key with `worker` tier or higher
4. A base VM template on your Proxmox node (see [Template Setup](#template-setup))

### Getting an API Key

Ask your Proxmox administrator to provision a client key:

```json
{
  "key": "<generated secret>",
  "name": "my-swe-squad",
  "tier": "worker",
  "vmid_start": 1100,
  "vmid_end": 1199,
  "max_vms": 3,
  "max_cpu": 8,
  "max_ram_gb": 16,
  "max_disk_gb": 80,
  "max_ttl_hours": 24
}
```

Generate a key: `python -c "import secrets; print(secrets.token_hex(32))"`

### Template Setup

The provider clones a base VM template for each sandbox. Create one with:

```bash
# Minimal Ubuntu 22.04 VM
# Must have:
#   - Python 3.11+
#   - git
#   - user 'agent' with SSH access
#   - your project pre-installed (or clone on startup)
# Once configured, convert to Proxmox template (right-click → Convert to Template)
```

Add the template VMID to your config:
```yaml
providers:
  sandbox:
    provider: proxmox
    template_vmid: 9000    # your template VMID
```

### Verifying Connectivity

```python
from src.swe_team.providers.sandbox.proxmox import ProxmoxSandbox

sandbox = ProxmoxSandbox(
    gateway_url="http://<gateway>:8080",
    api_key="<your-key>",
)

print(sandbox.health_check())    # True
print(sandbox.quota())           # usage vs limits
print(sandbox.cluster_status())  # available nodes
```

## Docker Provider

*(Coming soon)* — will use `docker run` to spin up isolated containers per ticket.

## Local Provider

The `local` provider runs commands directly on the host machine with no VM or
container. Use it for development or when no infrastructure is available.

```yaml
providers:
  sandbox:
    provider: local
```

No configuration required. Snapshots and rollbacks are no-ops.

## Implementing a Custom Provider

Create `src/swe_team/providers/sandbox/myprovider.py` implementing the
`SandboxProvider` protocol from `base.py`:

```python
from src.swe_team.providers.sandbox.base import SandboxProvider, SandboxSpec, SandboxInfo

class MyProvider:
    name = "myprovider"

    def create(self, spec: SandboxSpec) -> SandboxInfo: ...
    def status(self, sandbox_id: str) -> SandboxInfo: ...
    def run_command(self, sandbox_id: str, command: list) -> tuple[int, str, str]: ...
    def snapshot(self, sandbox_id: str, label: str) -> str: ...
    def rollback(self, sandbox_id: str, label: str) -> None: ...
    def delete(self, sandbox_id: str) -> None: ...
    def health_check(self) -> bool: ...

def from_config(cfg: dict) -> MyProvider:
    return MyProvider(...)
```

Then register it in `swe_team.yaml`:
```yaml
providers:
  sandbox:
    provider: myprovider
    # ... your config keys
```

No other changes needed. The `ProviderRegistry` loads it by name.

## Interface Reference

See [`src/swe_team/providers/sandbox/base.py`](../../src/swe_team/providers/sandbox/base.py)
for the full `SandboxProvider` protocol, `SandboxSpec`, and `SandboxInfo` dataclasses.
