"""Lifecycle of the runner container (``lyndrix-ai-runner``).

The plugin is pinned to the ``runner`` role: the instance that hosts it has
the host Docker socket (through the core socket manager) and the shared
storage. The runner container is long-lived — one per Docker host — and is
(re)created when the image, the credentials or the workspace path change.

Networking: the core instance itself runs in a container; the runner joins the
same Docker network so the plugin reaches the bridge by container name.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import socket
import subprocess
from pathlib import Path
from typing import Optional

from core.api import get_logger

from .bridge import BridgeClient

log = get_logger("Plugin:AI:Runner")

CONTAINER_NAME = "lyndrix-ai-runner"
BRIDGE_PORT = 8765
_CGROUP_ID_RE = re.compile(r"containers/([0-9a-f]{64})")


def _own_container_id() -> Optional[str]:
    """The id of the container this core runs in (mountinfo, then hostname)."""
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
        m = _CGROUP_ID_RE.search(text)
        if m:
            return m.group(1)
    except OSError:
        pass
    host = socket.gethostname()
    return host if re.fullmatch(r"[0-9a-f]{12,64}", host) else None


def _docker(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


class RunnerConfig:
    def __init__(self, *, image: str, auth_env: dict[str, str], bridge_token: str, model: Optional[str],
                 host_workspace: str, network: Optional[str]) -> None:
        self.image = image
        self.auth_env = auth_env            # exactly one of CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY
        self.bridge_token = bridge_token
        self.model = model
        self.host_workspace = host_workspace
        self.network = network

    def fingerprint(self) -> str:
        import hashlib

        raw = json.dumps({
            "image": self.image, "auth": sorted(self.auth_env.items()), "token": self.bridge_token,
            "model": self.model, "ws": self.host_workspace, "net": self.network,
        }, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


class RunnerManager:
    def __init__(self, socket_client, storage_dir: Path) -> None:
        self.socket_client = socket_client
        self.storage_dir = Path(storage_dir)
        self._fingerprint: Optional[str] = None
        self._host_storage: Optional[str] = None
        self.last_error: Optional[str] = None
        self.client: Optional[BridgeClient] = None

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def new_bridge_token() -> str:
        return secrets.token_urlsafe(32)

    async def host_storage_dir(self) -> str:
        """Host path of ``STORAGE_DIR`` (the runner mounts a sub-directory of it)."""
        if self._host_storage:
            return self._host_storage
        target = str(self.storage_dir)
        try:
            mounts = await self.socket_client.resolve_runtime_mounts([target])
            source = (mounts or {}).get(target)
            if source:
                self._host_storage = str(source)
                return self._host_storage
        except Exception as exc:
            log.warning(f"Runner: could not resolve the host path of {target} via the socket manager: {exc}")
        self._host_storage = target  # same path on host and in the container (bind mount)
        return self._host_storage

    @staticmethod
    def own_network() -> Optional[str]:
        cid = _own_container_id()
        if not cid:
            return None
        res = _docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", cid, timeout=15)
        if res.returncode != 0:
            return None
        try:
            nets = list(json.loads(res.stdout or "{}").keys())
        except ValueError:
            return None
        return nets[0] if nets else None

    @staticmethod
    def container_state() -> Optional[str]:
        """``running`` / ``exited`` / … or ``None`` when the container does not exist."""
        res = _docker("inspect", "-f", "{{.State.Status}}", CONTAINER_NAME, timeout=15)
        return res.stdout.strip() if res.returncode == 0 else None

    def bridge_url(self) -> str:
        return f"http://{CONTAINER_NAME}:{BRIDGE_PORT}"

    # ------------------------------------------------------------------ lifecycle

    async def ensure(self, cfg: RunnerConfig, *, force: bool = False) -> dict:
        """Start (or recreate) the runner so it matches ``cfg``; returns the health."""
        self.last_error = None
        self.client = BridgeClient(self.bridge_url(), cfg.bridge_token)
        state = await asyncio.to_thread(self.container_state)
        changed = self._fingerprint != cfg.fingerprint()
        try:
            if force or changed or state is None:
                if state is not None:
                    log.info(f"Runner: recreating {CONTAINER_NAME} ({'forced' if force else 'configuration changed' if changed else 'missing'})")
                    await asyncio.to_thread(_docker, "rm", "-f", CONTAINER_NAME)
                await self._spawn(cfg)
            elif state != "running":
                log.info(f"Runner: {CONTAINER_NAME} is {state} — starting it")
                res = await asyncio.to_thread(_docker, "start", CONTAINER_NAME)
                if res.returncode != 0:
                    await asyncio.to_thread(_docker, "rm", "-f", CONTAINER_NAME)
                    await self._spawn(cfg)
            self._fingerprint = cfg.fingerprint()
            return await self.wait_healthy()
        except Exception as exc:  # noqa: BLE001 — health shows it, setup never dies
            self.last_error = str(exc)[:500]
            log.error(f"Runner: ensure failed — {exc}")
            return {"status": "error", "error": self.last_error}

    async def _spawn(self, cfg: RunnerConfig) -> None:
        env = [{"key": k, "value": v} for k, v in cfg.auth_env.items()]
        env += [
            {"key": "LYNDRIX_AI_BRIDGE_TOKEN", "value": cfg.bridge_token},
            {"key": "LYNDRIX_AI_WORKSPACE", "value": "/workspace"},
            {"key": "PYTHONUNBUFFERED", "value": "1"},
        ]
        if cfg.model:
            env.append({"key": "LYNDRIX_AI_MODEL", "value": cfg.model})
        result = await self.socket_client.spawn_runner(
            image=cfg.image,
            name=CONTAINER_NAME,
            env_vars=env,
            mounts=[{"source": cfg.host_workspace, "target": "/workspace", "mode": "rw"}],
            command=None,
            remove=False,
            networks=[cfg.network] if cfg.network else [],
            timeout=180.0,
        )
        if not isinstance(result, dict) or result.get("status") not in ("running", "ok", "started"):
            raise RuntimeError(f"spawn failed: {(result or {}).get('error') if isinstance(result, dict) else result}")
        log.info(f"Runner: spawned {CONTAINER_NAME} from {cfg.image} on network {cfg.network or 'default'}")

    async def wait_healthy(self, attempts: int = 20, pause: float = 1.5) -> dict:
        last: dict = {"status": "starting"}
        for _ in range(attempts):
            try:
                health = await self.client.health()  # type: ignore[union-attr]
                if isinstance(health, dict):
                    return health
            except Exception as exc:  # noqa: BLE001
                last = {"status": "starting", "error": str(exc)[:200]}
            await asyncio.sleep(pause)
        self.last_error = last.get("error") or "bridge did not become healthy"
        return {"status": "error", "error": self.last_error}

    async def health(self) -> dict:
        if self.client is None:
            return {"status": "not_started"}
        try:
            return await self.client.health()
        except Exception as exc:  # noqa: BLE001
            return {"status": "unreachable", "error": str(exc)[:200]}

    async def stop(self) -> None:
        state = await asyncio.to_thread(self.container_state)
        if state is not None:
            await asyncio.to_thread(_docker, "stop", "-t", "10", CONTAINER_NAME, timeout=40)

    async def logs(self, tail: int = 200) -> str:
        res = await asyncio.to_thread(_docker, "logs", "--tail", str(tail), CONTAINER_NAME, timeout=20)
        return (res.stdout or "") + (res.stderr or "")
