from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import fakeredis
import pytest
import yaml
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[2]
GIB = 1024**3


def to_bytes(value: str) -> int:
    units = {"k": 1024, "m": 1024**2, "g": GIB}
    return int(float(value[:-1]) * units[value[-1].lower()])


def test_compose_mem_limits_total_under_six_gib():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    limits = {name: to_bytes(str(svc["mem_limit"])) for name, svc in compose["services"].items()}
    assert set(limits) == {"web", "worker", "postgres", "redis", "clickhouse", "minio"}
    assert sum(limits.values()) < 6 * GIB


def test_lite_compose_is_the_web_server_alone():
    compose = yaml.safe_load((ROOT / "docker-compose.lite.yml").read_text())
    assert list(compose["services"]) == ["web"]
    assert compose["services"]["web"]["environment"]["TRACELOOM_MODE"] == "lite"


@pytest.mark.parametrize("name", ["docker-compose.yml", "docker-compose.lite.yml"])
def test_compose_files_have_no_hardcoded_secrets(name):
    text = (ROOT / name).read_text()
    assert "sk-" not in text and "BEGIN" not in text


def test_lite_mode_never_imports_the_full_mode_clients(tmp_path):
    code = (
        "import sys\n"
        "from traceloom.config import Settings\n"
        "from traceloom.container import build_container\n"
        f"s = Settings(TRACELOOM_ENV='test', TRACELOOM_MODE='lite', DATABASE_URL='sqlite:///{tmp_path}/x.db')\n"
        "c = build_container(s)\n"
        "import traceloom.main\n"
        "full = ('boto3', 'redis', 'dramatiq', 'clickhouse_connect', 'traceloom.container_full')\n"
        "bad = [m for m in full if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    env = {**os.environ, "TRACELOOM_SERVER_SECRET": "k" * 32}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, env=env
    )
    assert result.returncode == 0, result.stderr


def test_configure_wires_the_full_adapters(monkeypatch, tmp_path):
    import clickhouse_connect
    import redis

    from traceloom.config import Settings
    from traceloom.container import Container
    from traceloom.container_full import configure

    class FakeClickHouse:
        def __init__(self):
            self.commands = []

        def command(self, sql, parameters=None):
            self.commands.append(sql)
            return 1

        def close(self):
            pass

    class FakeSweeper:
        def __init__(self, blobs, queue, min_age_seconds, interval_seconds):
            self.started = False

        def run_once(self, min_age=None):
            return 0

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

    fake_ch = FakeClickHouse()
    fake_redis = fakeredis.FakeRedis()
    monkeypatch.setattr(clickhouse_connect, "get_client", lambda **kw: fake_ch)
    monkeypatch.setattr(redis.Redis, "from_url", classmethod(lambda cls, url: fake_redis))
    module = types.ModuleType("traceloom.ingest.sweeper")
    module.Sweeper = FakeSweeper
    monkeypatch.setitem(sys.modules, "traceloom.ingest.sweeper", module)
    monkeypatch.setenv("TRACELOOM_SERVER_SECRET", "k" * 32)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    settings = Settings(
        TRACELOOM_ENV="test",
        TRACELOOM_MODE="full",
        DATABASE_URL=f"sqlite:///{tmp_path}/m.db",
        S3_BUCKET="tl-wiring",
    )
    container = Container(settings=settings)
    with mock_aws():
        configure(container)
        container.startup()  # creates the bucket and applies the ClickHouse schema
        assert container.blob_store.ping() is True
        container.blob_store.put("pending/p/a.json", b"{}")
        assert container.health_checks["blob"]() is True
    assert any("CREATE DATABASE" in c for c in fake_ch.commands)
    assert sum("CREATE TABLE" in c for c in fake_ch.commands) == 3
    assert container.limiter.hit("k", "1/minute").allowed
    container.cache.set("a", 1)
    assert container.cache.get("a") == 1
    assert container.health_checks["redis"]() and container.health_checks["clickhouse"]()
    assert callable(container.start_consumer)
    container.shutdown()
