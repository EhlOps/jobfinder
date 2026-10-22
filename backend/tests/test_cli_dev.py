"""`jobfinder dev` (#53) with every tool call faked: the command lines it would run, never a real cluster."""
import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from jobfinder.cli import dev
from tests.cli_runner import invoke_sync

RUNNING = [{"name": dev.CLUSTER, "serversRunning": 1, "serversCount": 1}]
STOPPED = [{"name": dev.CLUSTER, "serversRunning": 0, "serversCount": 1}]


def bindings(pg=5433, valkey=6380):
    """`docker inspect -f {{json .HostConfig.PortBindings}}` of the server node."""
    return json.dumps({"6443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "41234"}],
                       "30379/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(valkey)}],
                       "30432/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(pg)}]})


PORTS = bindings()


class Tools:
    """Records each command line; answers k3d's cluster list and docker port from its fields."""

    def __init__(self, clusters=(), ports=PORTS, fail=None):
        self.clusters, self.ports, self.fail = list(clusters), ports, fail
        self.calls: list[list[str]] = []
        self.kw: list[dict] = []

    def __call__(self, cmd, *, capture=False, check=True, **kw):
        self.calls.append(list(cmd))
        self.kw.append(kw)
        out, rc = "", 0
        if cmd[:3] == ["k3d", "cluster", "list"]:
            out = json.dumps(self.clusters)
        elif cmd[:2] == ["docker", "inspect"]:
            out = self.ports
        elif cmd[:3] == ["k3d", "kubeconfig", "get"]:
            out = "apiVersion: v1\nkind: Config\n"
        elif cmd[:3] == ["k3d", "cluster", "create"]:
            self.clusters = RUNNING
        if self.fail and cmd[: len(self.fail)] == self.fail:
            rc = 1
        if check and rc:
            raise dev.DevError(f"{cmd[0]} failed")
        return subprocess.CompletedProcess(cmd, rc, out, "")

    def find(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


@pytest.fixture
def tools(monkeypatch, tmp_path):
    for name in ("DEV_PG_PORT", "DEV_VALKEY_PORT", "DEV_API_PORT", "SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(dev.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dev, "port_free", lambda port: True)
    monkeypatch.setattr(dev, "pg_answers", lambda port: True)
    monkeypatch.setattr(dev, "valkey_answers", lambda port: True)
    supervised = []
    monkeypatch.setattr(dev, "supervise", lambda procs, env: supervised.append((procs, env)) or 0)
    t = Tools()
    t.supervised = supervised
    monkeypatch.setattr(dev, "sh", t)
    return t


def test_up_creates_the_cluster_with_its_own_kubeconfig(tools, tmp_path):
    r = invoke_sync("dev", "up")
    assert r.exit_code == 0, r.output
    [create] = tools.find("k3d", "cluster", "create")
    assert create[3] == dev.CLUSTER
    assert "--kubeconfig-update-default=false" in create and "--kubeconfig-switch-context=false" in create
    assert "127.0.0.1:5433:30432@server:0:direct" in create
    assert "127.0.0.1:6380:30379@server:0:direct" in create
    api = create[create.index("--api-port") + 1]
    assert api.startswith("127.0.0.1:") and api != "127.0.0.1:6443"
    assert "--disable=traefik@server:0" in create
    kubeconfig = tmp_path / ".config" / "jobfinder" / "k3d-kubeconfig"
    assert kubeconfig.read_text().startswith("apiVersion")
    assert kubeconfig.stat().st_mode & 0o777 == 0o600
    # Every kubectl call names that file and the dev context: never ~/.kube/config or its current context.
    kubectls = [c for c in tools.calls if c[0] == "kubectl"]
    assert kubectls and all(c[1:5] == ["--kubeconfig", str(kubeconfig), "--context", "k3d-jobfinder-dev"]
                            for c in kubectls)
    assert any("apply" in c and str(dev.MANIFESTS) in c for c in kubectls)
    assert not any("use-context" in c or "config" == c[1] for c in kubectls)


def test_up_migrates_then_supervises_the_app_against_k3d(tools):
    assert invoke_sync("dev").exit_code == 0  # bare `dev` is `up`
    [migrate] = tools.find(sys.executable, "-m", "alembic", "upgrade", "head")
    env = tools.kw[tools.calls.index(migrate)]["env"]
    assert tools.kw[tools.calls.index(migrate)]["cwd"] == dev.BACKEND
    assert env["DATABASE_URL"] == "postgresql+asyncpg://jobfinder:jobfinder@127.0.0.1:5433/jobfinder"
    assert env["VALKEY_URL"] == "redis://127.0.0.1:6380/0"
    assert env["PUBLIC_BASE_URL"] == "http://localhost:5173"
    [(procs, run_env)] = tools.supervised
    assert run_env == env
    names = dict(procs)
    assert list(names) == ["api", "scheduler", "worker-priority", "worker-ai", "worker-io"]
    assert "--reload" in names["api"] and names["api"][-2:] == ["--port", "8100"]
    assert names["worker-io"][-2].endswith("-m jobfinder.tasks io")
    assert names["worker-priority"][-2].endswith("-m jobfinder.tasks priority")


def test_ports_come_from_the_environment(tools, monkeypatch):
    monkeypatch.setenv("DEV_PG_PORT", "15433")
    monkeypatch.setenv("DEV_VALKEY_PORT", "16380")
    monkeypatch.setenv("DEV_API_PORT", "8188")
    tools.ports = bindings(15433, 16380)
    assert invoke_sync("dev", "up").exit_code == 0
    [create] = tools.find("k3d", "cluster", "create")
    assert "127.0.0.1:15433:30432@server:0:direct" in create
    [(procs, env)] = tools.supervised
    assert dict(procs)["api"][-1] == "8188" and ":15433/" in env["DATABASE_URL"]


def test_deps_only_skips_migrations_and_the_app(tools):
    r = invoke_sync("dev", "up", "--deps-only")
    assert r.exit_code == 0 and "TEST_ADMIN_URL" in r.stdout
    assert tools.find("kubectl")
    assert not tools.find(sys.executable) and not tools.supervised


def test_an_existing_cluster_is_reused_and_a_stopped_one_started(tools):
    tools.clusters = RUNNING
    assert invoke_sync("dev", "up", "--deps-only").exit_code == 0
    assert not tools.find("k3d", "cluster", "create") and not tools.find("k3d", "cluster", "start")
    tools.clusters = STOPPED
    assert invoke_sync("dev", "up", "--deps-only").exit_code == 0
    assert tools.find("k3d", "cluster", "start", dev.CLUSTER)


@pytest.mark.parametrize("state", [RUNNING, STOPPED])
def test_a_cluster_on_other_ports_is_refused(tools, monkeypatch, state):
    tools.clusters = state
    monkeypatch.setenv("DEV_PG_PORT", "15433")
    r = invoke_sync("dev", "up", "--deps-only")
    assert r.exit_code == 1 and "jobfinder dev down" in r.stderr
    assert not tools.find("kubectl") and not tools.find("k3d", "cluster", "start")


def test_the_secret_key_comes_from_backend_env_or_a_kept_dev_key(tools, monkeypatch, tmp_path):
    p = dev.Ports(5433, 6380, 8100)
    monkeypatch.setattr(dev, "BACKEND", tmp_path)
    (tmp_path / ".env").write_text("SECRET_KEY=\n")
    key = dev.app_env(p)["SECRET_KEY"]
    assert len(key) >= 32 and dev.app_env(p)["SECRET_KEY"] == key  # kept across runs
    (tmp_path / ".env").write_text("SECRET_KEY=from-the-dotenv\n")
    assert "SECRET_KEY" not in dev.app_env(p)  # the app reads backend/.env itself


def test_a_busy_port_is_refused_before_creating(tools, monkeypatch):
    monkeypatch.setattr(dev, "port_free", lambda port: port != 5433)
    r = invoke_sync("dev", "up")
    assert r.exit_code == 1 and "5433" in r.stderr and not tools.find("k3d", "cluster", "create")


class _Sock:
    """socket.socket() as port_free uses it, recording the options it sets."""

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def setsockopt(self, level, name, value):
        self.opts.append(name)

    def bind(self, address):
        pass


@pytest.mark.parametrize("platform,reuse", [("linux", True), ("darwin", False), ("freebsd14", False)])
def test_port_free_sets_reuseaddr_only_on_linux(monkeypatch, platform, reuse):
    """On macOS and the BSDs SO_REUSEADDR lets the bind succeed beside a listener on *:port."""
    import socket

    opts = []
    monkeypatch.setattr(dev.sys, "platform", platform)
    monkeypatch.setattr(dev.socket, "socket", lambda *a: _Sock(opts))
    assert dev.port_free(5433)
    assert (socket.SO_REUSEADDR in opts) is reuse


def test_port_free_sees_a_listener_on_every_address():
    import socket

    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        listener.listen()
        assert not dev.port_free(listener.getsockname()[1])


@pytest.mark.parametrize("verb", ["up", "status"])
def test_missing_k3d_prints_an_install_hint(tools, monkeypatch, verb):
    monkeypatch.setattr(dev.shutil, "which", lambda name: None if name == "k3d" else f"/usr/bin/{name}")
    r = invoke_sync("dev", verb)
    assert r.exit_code == 2 and "k3d" in r.stderr and "k3d.io" in r.stderr
    assert not tools.calls


def test_a_bad_port_is_a_usage_error(tools, monkeypatch):
    monkeypatch.setenv("DEV_API_PORT", "http")
    assert invoke_sync("dev", "up").exit_code == 2 and not tools.calls


def test_a_failing_step_exits_1(tools):
    tools.fail = ["kubectl"]
    r = invoke_sync("dev", "up")
    assert r.exit_code == 1 and "kubectl failed" in r.stderr and not tools.supervised


def test_down_deletes_the_cluster_and_its_kubeconfig(tools, tmp_path):
    tools.clusters = RUNNING
    kubeconfig = tmp_path / ".config" / "jobfinder" / "k3d-kubeconfig"
    kubeconfig.parent.mkdir(parents=True)
    kubeconfig.write_text("x")
    assert invoke_sync("dev", "down").exit_code == 0
    assert tools.find("k3d", "cluster", "delete", dev.CLUSTER) and not kubeconfig.exists()
    tools.clusters = []
    assert invoke_sync("dev", "down").exit_code == 0  # nothing to delete is fine


def test_status(tools):
    assert invoke_sync("dev", "status").exit_code == 1  # no cluster
    tools.clusters = RUNNING
    r = invoke_sync("dev", "status")
    assert r.exit_code == 0 and "127.0.0.1:5433" in r.stdout and "127.0.0.1:6380" in r.stdout
    assert any(c[0] == "kubectl" and "get" in c for c in tools.calls)


def test_dev_imports_nothing_of_the_app():
    """No settings or database module: the children load those, against the cluster dev just made."""
    tree = ast.parse(Path(dev.__file__).read_text())
    imported = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    imported += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not [m for m in imported if m and m.split(".")[0] == "jobfinder"]


def test_supervise_stops_everyone_when_one_exits():
    procs = [("quick", [sys.executable, "-c", "print('hi')"]),
             ("slow", [sys.executable, "-c", "import time; time.sleep(60)"])]
    assert dev.supervise(procs, {"PATH": ""}) == 1


def test_k3d_never_sees_the_default_kubeconfig(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KUBECONFIG", "/home/someone/.kube/config")
    seen = []
    monkeypatch.setattr(dev.subprocess, "run", lambda cmd, **kw: seen.append(kw.get("env"))
                        or subprocess.CompletedProcess(cmd, 0, "", ""))
    dev.sh(["k3d", "cluster", "delete", dev.CLUSTER])
    dev.sh(["docker", "ps"])
    assert seen[0]["KUBECONFIG"] == str(tmp_path / ".config" / "jobfinder" / "k3d-kubeconfig")
    assert seen[1] is None  # other tools inherit the environment unchanged
