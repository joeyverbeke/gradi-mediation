#!/usr/bin/env python3
"""Process supervisor for the gradi mediation stack."""

from __future__ import annotations

import argparse
import contextlib
import asyncio
import json
import os
import random
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - environment setup guard
        raise SystemExit(
            "TOML support is required. Install `tomli` for Python 3.10: uv pip install tomli."
        ) from exc

if sys.platform == "win32":  # pragma: no cover - Windows not targeted
    raise SystemExit("The supervisor is intended for POSIX platforms only.")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "services.toml"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_ts(ts: Optional[datetime]) -> Optional[str]:
    return ts.isoformat() if ts else None


def _expand_path(raw: Optional[str], *, base: Path) -> Optional[Path]:
    if not raw:
        return None
    expanded = Path(os.path.expanduser(raw))
    if not expanded.is_absolute():
        expanded = (base / expanded).resolve()
    return expanded


@dataclass(frozen=True)
class HealthProbe:
    probe_type: str
    url: Optional[str] = None
    fallback_urls: Sequence[str] = ()
    method: str = "GET"
    host: Optional[str] = None
    port: Optional[int] = None
    interval: float = 2.0
    timeout: float = 5.0
    expected_status: int = 200
    startup_grace: float = 5.0


@dataclass(frozen=True)
class RestartPolicy:
    initial_delay: float = 1.0
    max_delay: float = 60.0
    multiplier: float = 2.0
    jitter: float = 0.2
    reset_after: float = 300.0
    max_restarts: int = 0  # 0 => unlimited


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    command: Sequence[str]
    cwd: Path
    venv: Optional[Path]
    env: Mapping[str, str] = field(default_factory=dict)
    health: Optional[HealthProbe] = None
    restart: RestartPolicy = field(default_factory=RestartPolicy)
    grace_period: float = 20.0


@dataclass(frozen=True)
class SupervisorSpec:
    config_path: Path
    services: Sequence[ServiceSpec]
    log_dir: Path
    state_dir: Path
    default_grace_period: float


class ManifestError(RuntimeError):
    """Raised when the TOML manifest is invalid."""


def load_manifest(path: Path) -> SupervisorSpec:
    if not path.exists():
        raise ManifestError(f"Supervisor manifest not found: {path}")

    base_dir = path.parent.resolve()
    with path.open("rb") as handle:
        data = tomllib.load(handle)

    supervisor_raw = data.get("supervisor") or {}
    log_dir_raw = supervisor_raw.get("log_dir", "logs/services")
    state_dir_raw = supervisor_raw.get("state_dir", log_dir_raw)
    default_grace = float(supervisor_raw.get("default_grace_period", 20.0))

    log_dir = _expand_path(log_dir_raw, base=base_dir)
    state_dir = _expand_path(state_dir_raw, base=base_dir)
    if log_dir is None or state_dir is None:
        raise ManifestError("log_dir and state_dir must be set in the manifest.")

    raw_services = data.get("services")
    if not isinstance(raw_services, list) or not raw_services:
        raise ManifestError("Manifest must define at least one service.")

    seen_names: set[str] = set()
    services: List[ServiceSpec] = []
    for entry in raw_services:
        if not isinstance(entry, dict):
            raise ManifestError("Each service entry must be a table.")
        name = entry.get("name")
        if not name or not isinstance(name, str):
            raise ManifestError("Each service requires a string `name`.")
        if name in seen_names:
            raise ManifestError(f"Duplicate service name: {name}")
        seen_names.add(name)

        command = entry.get("command")
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise ManifestError(f"Service `{name}` must declare `command` as an array of strings.")

        cwd_raw = entry.get("cwd")
        if not isinstance(cwd_raw, str):
            raise ManifestError(f"Service `{name}` must define `cwd`.")
        cwd = _expand_path(cwd_raw, base=base_dir)
        if cwd is None:
            raise ManifestError(f"Service `{name}` has invalid `cwd` path.")

        venv = _expand_path(entry.get("venv"), base=base_dir)
        env = entry.get("env") or {}
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ManifestError(f"Service `{name}` env must be a table of string keys/values.")

        grace_period = float(entry.get("grace_period", default_grace))

        health_spec = entry.get("health")
        health: Optional[HealthProbe] = None
        if isinstance(health_spec, dict):
            probe_type = str(health_spec.get("type", "process")).lower()
            if probe_type not in {"http", "tcp", "process", "none"}:
                raise ManifestError(f"Service `{name}` health type `{probe_type}` is not supported.")
            if probe_type == "http":
                url = health_spec.get("url")
                if not isinstance(url, str):
                    raise ManifestError(f"Service `{name}` http health requires `url`.")
                fallback = health_spec.get("fallback_urls") or []
                if not isinstance(fallback, list) or not all(isinstance(item, str) for item in fallback):
                    raise ManifestError(f"Service `{name}` health.fallback_urls must be an array of strings.")
                health = HealthProbe(
                    probe_type="http",
                    url=url,
                    fallback_urls=tuple(fallback),
                    method=str(health_spec.get("method", "GET")).upper(),
                    interval=float(health_spec.get("interval", 2.0)),
                    timeout=float(health_spec.get("timeout", 5.0)),
                    expected_status=int(health_spec.get("expected_status", 200)),
                    startup_grace=float(health_spec.get("startup_grace", 5.0)),
                )
            elif probe_type == "tcp":
                host = health_spec.get("host")
                port = health_spec.get("port")
                if not isinstance(host, str) or not isinstance(port, int):
                    raise ManifestError(f"Service `{name}` tcp health requires `host` and `port`.")
                health = HealthProbe(
                    probe_type="tcp",
                    host=host,
                    port=port,
                    interval=float(health_spec.get("interval", 2.0)),
                    timeout=float(health_spec.get("timeout", 5.0)),
                    startup_grace=float(health_spec.get("startup_grace", 5.0)),
                )
            elif probe_type == "process":
                health = HealthProbe(
                    probe_type="process",
                    interval=float(health_spec.get("interval", 5.0)),
                    startup_grace=float(health_spec.get("startup_grace", 5.0)),
                )
            else:
                health = None

        restart_spec = entry.get("restart") or {}
        if not isinstance(restart_spec, dict):
            raise ManifestError(f"Service `{name}` restart policy must be a table.")
        restart = RestartPolicy(
            initial_delay=float(restart_spec.get("initial_delay", 1.0)),
            max_delay=float(restart_spec.get("max_delay", 60.0)),
            multiplier=float(restart_spec.get("multiplier", 2.0)),
            jitter=float(restart_spec.get("jitter", 0.2)),
            reset_after=float(restart_spec.get("reset_after", 300.0)),
            max_restarts=int(restart_spec.get("max_restarts", 0)),
        )

        services.append(
            ServiceSpec(
                name=name,
                command=tuple(command),
                cwd=cwd,
                venv=venv,
                env=env,
                health=health,
                restart=restart,
                grace_period=grace_period,
            )
        )

    return SupervisorSpec(
        config_path=path,
        services=tuple(services),
        log_dir=log_dir,
        state_dir=state_dir,
        default_grace_period=default_grace,
    )


class ServiceRuntime:
    """Runtime state for a managed service."""

    def __init__(self, spec: ServiceSpec, log_dir: Path, attach: bool) -> None:
        self.spec = spec
        self.log_dir = log_dir
        self.attach = attach
        self.process: Optional[asyncio.subprocess.Process] = None
        self.stdout_task: Optional[asyncio.Task[None]] = None
        self.stderr_task: Optional[asyncio.Task[None]] = None
        self.log_file_path = self.log_dir / f"{spec.name}.log"
        self.log_file_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_file_path.open("a", encoding="utf-8")
        self._shutdown_requested = False
        self._restart_attempt = 0
        self._last_start: Optional[datetime] = None
        self._last_exit: Optional[datetime] = None
        self._last_exit_code: Optional[int] = None
        self._last_signal: Optional[int] = None
        self._ready = False
        self._last_ready: Optional[datetime] = None
        self._health_failures = 0
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def ready(self) -> bool:
        return self._ready

    def reset_readiness(self) -> None:
        self._ready = False

    def update_readiness(self, ready: bool) -> None:
        self._ready = ready
        if ready:
            self._last_ready = _utc_now()
            self._health_failures = 0

    def mark_health_failure(self) -> None:
        self._health_failures += 1

    def get_health_failures(self) -> int:
        return self._health_failures

    def next_delay(self) -> float:
        policy = self.spec.restart
        if policy.max_restarts and self._restart_attempt >= policy.max_restarts:
            return -1.0
        base = policy.initial_delay * (policy.multiplier ** max(0, self._restart_attempt - 1))
        delay = min(policy.max_delay, base)
        jitter = delay * policy.jitter
        return max(0.1, delay + random.uniform(-jitter, jitter))

    def record_start(self) -> None:
        now = _utc_now()
        if self._last_start and self._last_exit:
            runtime = (self._last_exit - self._last_start).total_seconds()
            if runtime >= self.spec.restart.reset_after:
                self._restart_attempt = 0
        self._last_start = now
        self._restart_attempt += 1

    def record_exit(self, returncode: Optional[int]) -> None:
        self._last_exit = _utc_now()
        self._last_exit_code = returncode
        self.reset_readiness()
        self.process = None
        self.stdout_task = None
        self.stderr_task = None

    async def start(self) -> None:
        env = os.environ.copy()
        env.update(self.spec.env)
        if self.spec.venv:
            venv_path = self.spec.venv
            env["VIRTUAL_ENV"] = str(venv_path)
            bin_dir = venv_path / ("Scripts" if sys.platform == "win32" else "bin")
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        self._shutdown_requested = False
        command = list(self.spec.command)
        self.record_start()
        self._log_event("info", f"Starting: {' '.join(command)}", annotate=False)
        self.process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(self.spec.cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if self.process.stdout:
            self.stdout_task = asyncio.create_task(self._pipe_stream(self.process.stdout, "stdout"))
        if self.process.stderr:
            self.stderr_task = asyncio.create_task(self._pipe_stream(self.process.stderr, "stderr"))

    async def wait(self) -> int:
        if self.process is None:
            raise RuntimeError(f"Service {self.spec.name} has no active process.")
        return await self.process.wait()

    async def stop(self, *, reason: str, signal_name: str = "SIGINT") -> None:
        async with self._lock:
            if not self.is_running:
                return
            self._shutdown_requested = True
            sig = getattr(signal, signal_name)
            self._log_event("info", f"Sending {signal_name} for shutdown ({reason})")
            self.process.send_signal(sig)
            try:
                await asyncio.wait_for(self.process.wait(), timeout=self.spec.grace_period)
                return
            except asyncio.TimeoutError:
                self._log_event("warning", f"{signal_name} timed out; sending SIGTERM")
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=10.0)
                    return
                except asyncio.TimeoutError:
                    self._log_event("error", "SIGTERM timed out; forcing SIGKILL")
                    self.process.kill()
                    await self.process.wait()

    async def drain_outputs(self) -> None:
        tasks = [task for task in (self.stdout_task, self.stderr_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def close(self) -> None:
        try:
            self._log_handle.flush()
        finally:
            self._log_handle.close()

    def _log_event(self, level: str, message: str, *, annotate: bool = True) -> None:
        timestamp = _utc_now().isoformat()
        line = f"{timestamp} [{self.spec.name}] {level.upper()}: {message}"
        self._log_handle.write(line + "\n")
        self._log_handle.flush()
        if self.attach or annotate:
            print(line)

    async def _pipe_stream(self, stream: asyncio.StreamReader, stream_name: str) -> None:
        prefix = f"{self.spec.name}:{stream_name}"
        try:
            while True:
                chunk = await stream.readline()
                if not chunk:
                    break
                text = chunk.decode(errors="replace").rstrip("\n")
                timestamp = _utc_now().isoformat()
                line = f"{timestamp} [{prefix}] {text}"
                self._log_handle.write(line + "\n")
                if self.attach:
                    print(line)
        finally:
            self._log_handle.flush()


class Supervisor:
    """Ensure managed services stay running and report status."""

    def __init__(self, spec: SupervisorSpec, *, attach: Iterable[str] = ()) -> None:
        self.spec = spec
        self.attach = set(attach)
        self.state_path = self.spec.state_dir / "supervisor_state.json"
        self.pid_path = self.spec.state_dir / "supervisor.pid"
        self.spec.log_dir.mkdir(parents=True, exist_ok=True)
        self.spec.state_dir.mkdir(parents=True, exist_ok=True)
        self.services: Dict[str, ServiceRuntime] = {
            svc.name: ServiceRuntime(svc, self.spec.log_dir, svc.name in self.attach) for svc in spec.services
        }
        self._shutdown_event = asyncio.Event()
        self._service_tasks: Dict[str, asyncio.Task[None]] = {}
        self._supervisor_started = _utc_now()
        self._write_state("initializing")

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self._request_shutdown(f"signal:{s.name}")))

        self._write_state("starting")
        for name, runtime in self.services.items():
            task = asyncio.create_task(self._service_loop(name, runtime))
            self._service_tasks[name] = task

        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        try:
            await self._shutdown_event.wait()
        finally:
            await self._shutdown_services(reason="supervisor shutdown")
            await asyncio.gather(*self._service_tasks.values(), return_exceptions=True)
            self._write_state("stopped")
            with contextlib.suppress(FileNotFoundError):
                self.pid_path.unlink()
        return 0

    async def _service_loop(self, name: str, runtime: ServiceRuntime) -> None:
        while not self._shutdown_event.is_set():
            try:
                await runtime.start()
            except FileNotFoundError as exc:
                runtime._log_event("error", f"Failed to start ({exc})")
                self._record_state(name, status="error", message=str(exc))
                self._shutdown_event.set()
                return
            except Exception as exc:  # pragma: no cover - defensive
                runtime._log_event("error", f"Unexpected start failure: {exc}")
                self._record_state(name, status="error", message=str(exc))
                self._shutdown_event.set()
                return

            self._record_state(name, status="starting", pid=runtime.process.pid if runtime.process else None)

            if runtime.spec.health:
                await self._await_health(runtime)
            else:
                runtime.update_readiness(True)
                self._record_state(name, status="running", ready=True)

            if runtime.process is None:
                return

            exit_code = await runtime.wait()
            await runtime.drain_outputs()
            runtime.record_exit(exit_code)
            runtime._log_event("info", f"Process exited with code {exit_code}")
            self._record_state(
                name,
                status="exited",
                ready=False,
                pid=None,
                exit_code=exit_code,
                restarted=runtime._restart_attempt,
            )

            if runtime._shutdown_requested or self._shutdown_event.is_set():
                return

            delay = runtime.next_delay()
            if delay < 0:
                runtime._log_event("error", "Restart limit reached; marking as failed.")
                self._record_state(name, status="failed", ready=False)
                self._shutdown_event.set()
                return

            runtime._log_event("warning", f"Restarting in {delay:.1f}s (attempt {runtime._restart_attempt})")
            await asyncio.sleep(delay)

    async def _await_health(self, runtime: ServiceRuntime) -> None:
        probe = runtime.spec.health
        if probe is None:
            runtime.update_readiness(True)
            self._record_state(runtime.spec.name, status="running", ready=True)
            return

        if probe.probe_type == "process":
            runtime.update_readiness(True)
            runtime._log_event("info", "Process health probe configured; marking service ready.")
            self._record_state(runtime.spec.name, status="running", ready=True)
            return

        start = time.monotonic()
        await asyncio.sleep(max(0.0, probe.startup_grace))
        while True:
            if self._shutdown_event.is_set() or not runtime.is_running:
                runtime.mark_health_failure()
                self._record_state(runtime.spec.name, status="starting", ready=False)
                return

            ok = await self._execute_probe(probe)
            if ok:
                runtime.update_readiness(True)
                runtime._log_event("info", "Health probe succeeded")
                self._record_state(runtime.spec.name, status="running", ready=True)
                return

            runtime.mark_health_failure()
            elapsed = time.monotonic() - start
            runtime._log_event("warning", f"Health probe failed ({runtime.get_health_failures()} attempts, elapsed {elapsed:.1f}s)")
            self._record_state(runtime.spec.name, status="starting", ready=False)
            await asyncio.sleep(max(0.5, probe.interval))

    async def _execute_probe(self, probe: HealthProbe) -> bool:
        if probe.probe_type == "process":
            return True
        if probe.probe_type == "http":
            return await asyncio.to_thread(_http_probe, probe)
        if probe.probe_type == "tcp":
            return await asyncio.to_thread(_tcp_probe, probe)
        return True

    async def _shutdown_services(self, *, reason: str) -> None:
        for runtime in self.services.values():
            try:
                await runtime.stop(reason=reason)
            except Exception as exc:  # pragma: no cover - defensive
                runtime._log_event("error", f"Error during shutdown: {exc}")
            finally:
                await runtime.drain_outputs()
                runtime.close()
        self._write_state("stopping")

    async def _request_shutdown(self, source: str) -> None:
        if not self._shutdown_event.is_set():
            print(f"[supervisor] shutdown requested via {source}")
            self._shutdown_event.set()

    def _record_state(
        self,
        service_name: str,
        *,
        status: str,
        pid: Optional[int] = None,
        ready: Optional[bool] = None,
        exit_code: Optional[int] = None,
        message: Optional[str] = None,
        restarted: Optional[int] = None,
    ) -> None:
        state = self._load_state()
        svc_state = state.setdefault("services", {}).setdefault(service_name, {})
        svc_state.update(
            {
                "status": status,
                "pid": pid,
                "ready": ready,
                "exit_code": exit_code,
                "message": message,
                "restarts": restarted,
                "updated_at": _utc_now().isoformat(),
            }
        )
        state["updated_at"] = _utc_now().isoformat()
        self._write_state("running", state_override=state)

    def _load_state(self) -> Dict[str, Any]:
        if self.state_path.exists():
            try:
                with self.state_path.open("r", encoding="utf-8") as handle:
                    return json.load(handle)
            except json.JSONDecodeError:
                pass
        return {}

    def _write_state(self, status: str, *, state_override: Optional[Dict[str, Any]] = None) -> None:
        state = state_override or self._load_state()
        state.update(
            {
                "supervisor_pid": os.getpid(),
                "status": status,
                "config_path": str(self.spec.config_path),
                "started_at": _format_ts(self._supervisor_started),
                "updated_at": _utc_now().isoformat(),
            }
        )
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.state_path.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")


def _http_probe(probe: HealthProbe) -> bool:
    import urllib.error
    import urllib.request

    urls = [probe.url] if probe.url else []
    urls.extend(probe.fallback_urls)
    for url in urls:
        if url is None:
            continue
        try:
            request = urllib.request.Request(url, method=probe.method)
            with urllib.request.urlopen(request, timeout=probe.timeout) as response:
                status = response.getcode()
                if status == probe.expected_status:
                    return True
                if status == 200 and probe.expected_status != 200:
                    return True
        except urllib.error.URLError:
            continue
    return False


def _tcp_probe(probe: HealthProbe) -> bool:
    import socket

    if probe.host is None or probe.port is None:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(probe.timeout)
    try:
        sock.connect((probe.host, probe.port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def load_state_from_manifest(manifest_path: Path) -> Dict[str, Any]:
    spec = load_manifest(manifest_path)
    state_path = spec.state_dir / "supervisor_state.json"
    if not state_path.exists():
        return {}
    with state_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def command_status(args: argparse.Namespace) -> int:
    state = load_state_from_manifest(args.config)
    if not state:
        print("Supervisor state file not found or empty.")
        return 1
    print(json.dumps(state, indent=2, sort_keys=True))
    return 0


def command_down(args: argparse.Namespace) -> int:
    state = load_state_from_manifest(args.config)
    if not state:
        print("No running supervisor found.")
        return 1
    pid = state.get("supervisor_pid")
    if not isinstance(pid, int):
        print("Supervisor PID missing from state file.")
        return 1
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        print(f"Supervisor PID {pid} is not running.")
        return 1
    print(f"Sent SIGINT to supervisor PID {pid}. Waiting for shutdown...")
    for _ in range(args.timeout):
        time.sleep(1.0)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print("Supervisor stopped.")
            return 0
    print("Supervisor did not stop within timeout.")
    return 1


def command_logs(args: argparse.Namespace) -> int:
    spec = load_manifest(args.config)
    runtime = next((svc for svc in spec.services if svc.name == args.service), None)
    if runtime is None:
        print(f"Service `{args.service}` not defined in manifest.")
        return 1
    log_path = spec.log_dir / f"{runtime.name}.log"
    if not log_path.exists():
        print(f"No log file for {runtime.name} at {log_path}")
        return 1
    with log_path.open("r", encoding="utf-8") as handle:
        lines = handle.readlines()
    tail = lines[-args.lines :]
    for line in tail:
        print(line.rstrip("\n"))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Launch and supervise the gradi mediation services.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to services manifest (TOML).")
    subparsers = parser.add_subparsers(dest="command", required=True)

    up_parser = subparsers.add_parser("up", help="Start the supervisor and all services.")
    up_parser.add_argument(
        "--attach",
        action="append",
        default=[],
        metavar="SERVICE",
        help="Stream stdout/stderr for the named service to the console (repeatable).",
    )

    subparsers.add_parser("status", help="Print supervisor state JSON.")

    down_parser = subparsers.add_parser("down", help="Gracefully stop the running supervisor.")
    down_parser.add_argument("--timeout", type=int, default=60, help="Seconds to wait for shutdown (default: 60)")

    logs_parser = subparsers.add_parser("logs", help="Show recent log lines for a service.")
    logs_parser.add_argument("service", help="Service name.")
    logs_parser.add_argument("--lines", type=int, default=40, help="Number of log lines to show (default: 40).")

    args = parser.parse_args(argv)

    if args.command == "status":
        return command_status(args)
    if args.command == "down":
        return command_down(args)
    if args.command == "logs":
        return command_logs(args)

    spec = load_manifest(args.config)
    supervisor = Supervisor(spec, attach=args.attach)
    return asyncio.run(supervisor.run())


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
