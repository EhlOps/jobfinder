"""`jobfinder dev [up|down|status]`: the local dev loop (#53). Bare `jobfinder dev` is `up`.

k3d (k3s in Docker) runs Postgres and valkey from deploy/dev in a cluster named jobfinder-dev, their
NodePorts mapped to 127.0.0.1:DEV_PG_PORT (5433) and DEV_VALKEY_PORT (6380). `up` then migrates and runs
the api (DEV_API_PORT, 8100, where vite proxies /api), the scheduler and the ai/io workers on the host
with reload, their output prefixed by name; Ctrl-C stops them all. The cluster and its volumes outlive
`up`; `down` deletes them. The cluster's kubeconfig is its own file (KUBECONFIG_PATH): this never reads
or changes ~/.kube/config or its current context.

Deliberately light: it imports no settings or database module, so it works before either exists.
"""
import json
import os
import secrets
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, NamedTuple

import typer

CLUSTER = "jobfinder-dev"
CONTEXT = f"k3d-{CLUSTER}"  # the context name in k3d's kubeconfig
NODE = f"k3d-{CLUSTER}-server-0"  # the k3s server's container
NAMESPACE = "jobfinder-dev"  # deploy/dev/kustomization.yaml
K3S_IMAGE = "rancher/k3s:v1.31.5-k3s1"  # k3d 5.8.3's default, pinned so a k3d upgrade doesn't move it
PG_NODEPORT, VALKEY_NODEPORT = 30432, 30379  # deploy/dev/{postgres,valkey}.yaml
BACKEND = Path(__file__).resolve().parents[2]
MANIFESTS = BACKEND.parent / "deploy" / "dev"
TOOLS = {
    "docker": "https://docs.docker.com/engine/install/",
    "k3d": "https://k3d.io/stable/#installation (a release binary in ~/.local/bin is enough)",
    "kubectl": "https://kubernetes.io/docs/tasks/tools/#kubectl",
}
STOP_GRACE = 60  # after SIGTERM, before SIGKILL: over the workers' 50s --wait-tasks-timeout, like the pods

app = typer.Typer(help="Local dev: Postgres and valkey in k3d, the app on the host with reload. "
                       "Bare `jobfinder dev` is `up`.")


class DevError(Exception):
    """A step failed: its message goes to stderr and the command exits 1."""


class Ports(NamedTuple):
    pg: int
    valkey: int
    api: int


def kubeconfig_path() -> Path:
    return Path.home() / ".config" / "jobfinder" / "k3d-kubeconfig"


def ports() -> Ports:
    """DEV_PG_PORT, DEV_VALKEY_PORT, DEV_API_PORT: dev-tool environment, not app settings."""
    values = []
    for name, default in (("DEV_PG_PORT", 5433), ("DEV_VALKEY_PORT", 6380), ("DEV_API_PORT", 8100)):
        raw = os.environ.get(name) or str(default)
        if not raw.isdigit() or not 0 < int(raw) < 65536:
            typer.echo(f"{name}={raw!r} is not a port number", err=True)
            raise typer.Exit(2)
        values.append(int(raw))
    return Ports(*values)


def sh(cmd: list[str], *, capture: bool = False, check: bool = True, **kw) -> subprocess.CompletedProcess:
    """Run one tool; a non-zero exit is a DevError unless check=False.

    k3d always sees KUBECONFIG as the dev file: `k3d cluster delete` otherwise rewrites ~/.kube/config
    (it has no flag to leave it alone), even for a cluster that was never in it.
    """
    if cmd[0] == "k3d":
        kw["env"] = {**kw.get("env", os.environ), "KUBECONFIG": str(kubeconfig_path())}
    r = subprocess.run(cmd, text=True, capture_output=capture, check=False, **kw)
    if check and r.returncode != 0:
        detail = (r.stderr or "").strip() if capture else ""
        raise DevError(f"`{shlex.join(cmd)}` exited {r.returncode}" + (f": {detail}" if detail else ""))
    return r


def kubectl(*args: str) -> list[str]:
    """A kubectl command line pinned to the dev cluster's own kubeconfig and context, never the default."""
    return ["kubectl", "--kubeconfig", str(kubeconfig_path()), "--context", CONTEXT, *args]


def require_tools(*names: str) -> None:
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        typer.echo("jobfinder dev needs " + ", ".join(missing) + " on PATH. Install:", err=True)
        for n in missing:
            typer.echo(f"  {n}: {TOOLS[n]}", err=True)
        raise typer.Exit(2)


def cluster() -> dict | None:
    """The jobfinder-dev entry of `k3d cluster list`, or None when there is no such cluster."""
    out = sh(["k3d", "cluster", "list", "-o", "json"], capture=True).stdout
    return next((c for c in json.loads(out or "[]") if c.get("name") == CLUSTER), None)


def running(c: dict) -> bool:
    return c.get("serversRunning", 0) >= 1


def mapped_ports() -> dict[int, int]:
    """{container port: host port} of the server node, from its port bindings (set even while stopped)."""
    out = sh(["docker", "inspect", "-f", "{{json .HostConfig.PortBindings}}", NODE], capture=True).stdout
    mapped = {}
    for inner, hosts in (json.loads(out or "null") or {}).items():  # {"30432/tcp": [{"HostPort": "5433"}]}
        if inner.endswith("/tcp") and hosts:
            mapped[int(inner.split("/")[0])] = int(hosts[0]["HostPort"])
    return mapped


def port_free(port: int) -> bool:
    with socket.socket() as s:
        # TIME_WAIT leftovers don't hold a port (#53). Linux only: on macOS and the BSDs SO_REUSEADDR also
        # lets this bind beside a listener on *:port, which would report a busy port free.
        if sys.platform.startswith("linux"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_port() -> int:
    """An unused ephemeral port for the k3s API (never 6443: the host may run its own k3s there)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def require_free(p: Ports) -> None:
    busy = [str(port) for port in (p.pg, p.valkey) if not port_free(port)]
    if busy:
        raise DevError(f"127.0.0.1:{', '.join(busy)} already in use; free it or set DEV_PG_PORT / "
                       "DEV_VALKEY_PORT")


def create_cluster(p: Ports) -> None:
    require_free(p)
    sh(["k3d", "cluster", "create", CLUSTER,
        "--image", K3S_IMAGE, "--servers", "1", "--agents", "0",
        "--no-lb",  # no proxy container: the ports map straight onto the server node (:direct)
        "--api-port", f"127.0.0.1:{free_port()}",
        "-p", f"127.0.0.1:{p.pg}:{PG_NODEPORT}@server:0:direct",
        "-p", f"127.0.0.1:{p.valkey}:{VALKEY_NODEPORT}@server:0:direct",
        "--k3s-arg", "--disable=traefik@server:0",
        "--k3s-arg", "--disable=metrics-server@server:0",
        "--k3s-arg", "--disable=servicelb@server:0",
        "--kubeconfig-update-default=false", "--kubeconfig-switch-context=false",
        "--wait", "--timeout", "180s"])


def write_kubeconfig() -> None:
    config = sh(["k3d", "kubeconfig", "get", CLUSTER], capture=True).stdout
    path = kubeconfig_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(config)


def ensure_cluster(p: Ports) -> None:
    """Create jobfinder-dev, or start it when stopped; its port mapping must match the requested ports."""
    c = cluster()
    if c is None:
        typer.echo(f"Creating k3d cluster {CLUSTER} ...")
        create_cluster(p)
    else:
        mapped = mapped_ports()
        if mapped.get(PG_NODEPORT) != p.pg or mapped.get(VALKEY_NODEPORT) != p.valkey:
            raise DevError(f"{CLUSTER} maps Postgres to {mapped.get(PG_NODEPORT)} and valkey to "
                           f"{mapped.get(VALKEY_NODEPORT)}, not {p.pg} and {p.valkey}; ports are fixed when "
                           "the cluster is created, so run `jobfinder dev down` first (it deletes the dev data)")
        if not running(c):
            require_free(p)
            typer.echo(f"Starting k3d cluster {CLUSTER} ...")
            sh(["k3d", "cluster", "start", CLUSTER, "--wait", "--timeout", "180s"])
    write_kubeconfig()


def pg_answers(port: int) -> bool:
    """Postgres itself answers an SSLRequest with one byte, N or S (a bare docker port only accepts)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
            s.sendall(struct.pack("!ii", 8, 80877103))
            return s.recv(1) in (b"N", b"S")
    except OSError:
        return False


def valkey_answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
            s.sendall(b"PING\r\n")
            return s.recv(16).startswith(b"+PONG")
    except OSError:
        return False


def deploy_deps(p: Ports, timeout: float = 120) -> None:
    """Apply deploy/dev, wait for both StatefulSets, then until both answer on the host ports."""
    sh(kubectl("apply", "-k", str(MANIFESTS)))
    for name in ("postgres", "valkey"):
        sh(kubectl("-n", NAMESPACE, "rollout", "status", f"statefulset/{name}", "--timeout=300s"))
    deadline = time.monotonic() + timeout
    while not (pg_answers(p.pg) and valkey_answers(p.valkey)):
        if time.monotonic() > deadline:
            raise DevError(f"Postgres (127.0.0.1:{p.pg}) or valkey (127.0.0.1:{p.valkey}) did not answer "
                           f"within {timeout:.0f}s; see `jobfinder dev status`")
        time.sleep(1)


def app_env(p: Ports) -> dict[str, str]:
    """The children's environment: real variables win over backend/.env, so these point them at k3d."""
    env = dict(os.environ)
    env.update(
        DATABASE_URL=f"postgresql+asyncpg://jobfinder:jobfinder@127.0.0.1:{p.pg}/jobfinder",
        VALKEY_URL=f"redis://127.0.0.1:{p.valkey}/0",
        PUBLIC_BASE_URL="http://localhost:5173",  # vite's dev server, which proxies /api here
        PYTHONUNBUFFERED="1",  # so prefixed lines show up as they are written
    )
    if not env.get("SECRET_KEY") and not has_secret_key(BACKEND / ".env"):
        env["SECRET_KEY"] = dev_secret_key()  # a fresh checkout has no backend/.env yet
    return env


def has_secret_key(dotenv: Path) -> bool:
    try:
        lines = dotenv.read_text().splitlines()
    except OSError:
        return False
    return any(line.startswith("SECRET_KEY=") and line.partition("=")[2].strip() for line in lines)


def dev_secret_key() -> str:
    """One random key kept next to the kubeconfig, so dev sessions survive restarts."""
    path = kubeconfig_path().with_name("dev-secret-key")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(48))
    return path.read_text().strip()


def watched(argv: list[str]) -> list[str]:
    """argv under watchfiles: restarted when a .py file under jobfinder/ changes."""
    return [sys.executable, "-m", "watchfiles", "--filter", "python", "--target-type", "command",
            shlex.join(argv), "jobfinder"]


def processes(p: Ports) -> list[tuple[str, list[str]]]:
    py = sys.executable
    return [
        ("api", [py, "-m", "uvicorn", "jobfinder.api.app:create_app", "--factory", "--reload",
                 "--reload-dir", "jobfinder", "--host", "127.0.0.1", "--port", str(p.api)]),
        ("scheduler", watched([py, "-m", "jobfinder.scheduler"])),
        ("worker-ai", watched([py, "-m", "jobfinder.tasks", "ai"])),
        ("worker-io", watched([py, "-m", "jobfinder.tasks", "io"])),
    ]


def migrate(env: dict[str, str]) -> None:
    typer.echo("Migrating (alembic upgrade head) ...")
    sh([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env)


def _pump(name: str, stream, width: int) -> None:
    for line in stream:
        sys.stdout.write(f"{name:<{width}} | {line}")
        sys.stdout.flush()


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except OSError:  # the group is gone (ESRCH; EPERM on macOS for a group of zombies)
        pass


def supervise(procs: list[tuple[str, list[str]]], env: dict[str, str]) -> int:
    """Run the children until SIGINT/SIGTERM (exit 0) or until one exits by itself (exit 1), then stop all.

    Each child leads its own process group, so a terminal's Ctrl-C reaches only us and we stop them in
    order: SIGTERM to every group (the reloaders and their workers), SIGKILL after STOP_GRACE or on a
    second Ctrl-C.
    """
    stop = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    width = max(len(name) for name, _ in procs)
    children: list[tuple[str, subprocess.Popen]] = []
    pumps: list[threading.Thread] = []
    code = 0
    try:
        for name, argv in procs:
            proc = subprocess.Popen(argv, cwd=BACKEND, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                    bufsize=1, start_new_session=True)
            children.append((name, proc))
            pump = threading.Thread(target=_pump, args=(name, proc.stdout, width), daemon=True)
            pump.start()
            pumps.append(pump)
        while not stop.wait(0.5):
            exited = [(name, proc.returncode) for name, proc in children if proc.poll() is not None]
            if exited:
                for name, rc in exited:
                    typer.echo(f"{name} exited with code {rc}; stopping the others", err=True)
                code = 1
                break
    finally:
        force = threading.Event()  # a second Ctrl-C skips the grace
        for sig in previous:
            signal.signal(sig, lambda *_: force.set())
        for _, proc in children:
            if proc.poll() is None:
                _signal_group(proc, signal.SIGTERM)
        deadline = time.monotonic() + STOP_GRACE
        while any(proc.poll() is None for _, proc in children) and time.monotonic() < deadline:
            if force.wait(0.2):
                break
        for name, proc in children:
            if proc.poll() is None:
                typer.echo(f"{name} still running; killing it", err=True)
                _signal_group(proc, signal.SIGKILL)
            proc.wait()
            _signal_group(proc, signal.SIGKILL)  # whatever its reloader left behind in the group
        for pump in pumps:
            pump.join(timeout=2)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return code


@app.callback(invoke_without_command=True)
def default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        up(deps_only=False)


@contextmanager
def reported() -> Iterator[None]:
    """A DevError's message goes to stderr and the command exits 1."""
    try:
        yield
    except DevError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None


@app.command("up")
def up(deps_only: Annotated[bool, typer.Option(
        "--deps-only", help="Only ensure the cluster with Postgres and valkey (e.g. for pytest), then exit.",
)] = False) -> None:
    """Start the cluster, migrate, then run the api, scheduler and workers until Ctrl-C."""
    require_tools("k3d", "kubectl", "docker")
    p = ports()
    with reported():
        ensure_cluster(p)
        deploy_deps(p)
        typer.echo(f"Postgres on 127.0.0.1:{p.pg} (jobfinder/jobfinder), valkey on 127.0.0.1:{p.valkey}")
        if deps_only:
            typer.echo(f"Tests: TEST_ADMIN_URL=postgresql+asyncpg://jobfinder:jobfinder@localhost:{p.pg}"
                       "/postgres uv run pytest")
            return
        env = app_env(p)
        migrate(env)
    typer.echo(f"api on http://127.0.0.1:{p.api} (frontend: npm run dev, then http://localhost:5173); "
               "Ctrl-C stops everything")
    raise typer.Exit(supervise(processes(p), env))


@app.command("down")
def down() -> None:
    """Delete the cluster, its Postgres and valkey data, and its kubeconfig file."""
    require_tools("k3d")
    with reported():
        if cluster() is not None:
            sh(["k3d", "cluster", "delete", CLUSTER])
        else:
            typer.echo(f"No {CLUSTER} cluster")
    kubeconfig_path().unlink(missing_ok=True)


@app.command("status")
def status() -> None:
    """The cluster, its pods and the host ports Postgres and valkey are on."""
    require_tools("k3d", "kubectl", "docker")
    with reported():
        c = cluster()
        if c is None:
            typer.echo(f"No {CLUSTER} cluster (start it with `jobfinder dev up`)")
            raise typer.Exit(1)
        mapped = mapped_ports()
        ports_line = (f"Postgres 127.0.0.1:{mapped.get(PG_NODEPORT)}, "
                      f"valkey 127.0.0.1:{mapped.get(VALKEY_NODEPORT)}")
        if not running(c):
            typer.echo(f"{CLUSTER}: stopped ({ports_line}; `jobfinder dev up` starts it)")
            return
        typer.echo(f"{CLUSTER}: running; {ports_line}; kubeconfig {kubeconfig_path()}")
        write_kubeconfig()
        sh(kubectl("-n", NAMESPACE, "get", "pods,pvc", "-o", "wide"))
