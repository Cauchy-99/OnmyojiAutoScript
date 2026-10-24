import importlib.util
from pathlib import Path


def test_smoke_module_imports():
    path = Path("scripts/agent_smoke.py")
    assert path.exists()
    spec = importlib.util.spec_from_file_location("agent_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "fetch")
    assert callable(module.main)


def test_playbook_and_readme_exist():
    assert Path("config/agent/playbook.md").exists()
    assert Path("docs/agent/README.md").exists()
    assert "不要改任何源码" in Path("config/agent/playbook.md").read_text(encoding="utf-8")
