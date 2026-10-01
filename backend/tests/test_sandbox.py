"""The sandbox must hold even when static verification is bypassed."""
import pandas as pd
import pytest

from excel_copilot.sandbox import executor
from excel_copilot.sandbox.verify import VerificationError, verify

T = {"t": pd.DataFrame({"a": [1, 2, 3]})}

ATTACKS = {
    "import": "def run(tables):\n    import os\n    return os.system('id')\n",
    "open": "def run(tables):\n    return open('/etc/passwd').read()\n",
    "read file via pandas": "def run(tables):\n    return pd.read_csv('/etc/passwd')\n",
    "network via pandas": "def run(tables):\n    return pd.read_csv('http://169.254.169.254/latest/meta-data/')\n",
    "write file": "def run(tables):\n    tables['t'].to_csv('/tmp/pwned.csv')\n    return tables['t']\n",
    "pandas eval": "def run(tables):\n    return tables['t'].eval('a + 1').to_frame()\n",
    "dunder walk": "def run(tables):\n    return ().__class__.__base__.__subclasses__()\n",
    "builtins via globals": "def run(tables):\n    return run.__globals__['__builtins__']\n",
}


@pytest.fixture
def unverified(monkeypatch):
    monkeypatch.setattr(executor, "verify", lambda src: None)
    return executor.execute


@pytest.mark.parametrize("name", list(ATTACKS))
def test_verifier_rejects(name):
    with pytest.raises(VerificationError):
        verify(ATTACKS[name])


@pytest.mark.parametrize("name", list(ATTACKS))
def test_runtime_blocks_even_without_verifier(unverified, name):
    res = unverified(ATTACKS[name], T, executor.Limits(timeout_s=30))
    assert not res.ok, f"{name} succeeded: {res.frame}"


def test_cpu_and_wall_limits(unverified):
    src = "def run(tables):\n    while True:\n        pass\n"
    res = unverified(src, T, executor.Limits(timeout_s=8, cpu_seconds=2))
    assert not res.ok and ("CPU" in res.error or "killed" in res.error or "timed out" in res.error)


def test_memory_limit(unverified):
    src = "def run(tables):\n    return pd.DataFrame({'a': range(10**9)})\n"
    res = unverified(src, T, executor.Limits(memory_bytes=1024**3))
    assert not res.ok and "Memory" in res.error


def test_child_environment_has_no_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    env = executor._child_env()
    assert not any("ANTHROPIC" in k or k.startswith("AWS") for k in env)


def test_result_is_json_not_pickle_and_types_survive():
    src = ("def run(tables):\n    t = tables['t'].copy()\n"
           "    u = xl.with_column(t, 'd', pd.Timestamp('2024-01-02'))\n    return u\n")
    res = executor.execute(src, T)
    assert res.ok and list(res.frame.columns) == ["a", "d"]
    assert str(res.frame["d"].dtype).startswith("datetime64")
