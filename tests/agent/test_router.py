from fastapi import FastAPI
from fastapi.testclient import TestClient

from module.server.agent.logtail import LogTailer
from module.server.agent.memory import AgentMemory
from module.server.agent.service import AgentService
from module.server.agent_router import create_agent_router
from tests.agent.fakes import FakeManager


def _client(tmp_path, manager=None):
    manager = manager or FakeManager()
    service = AgentService(
        manager, memory=AgentMemory(root=tmp_path), tailer=LogTailer(root=tmp_path)
    )
    app = FastAPI()
    app.include_router(create_agent_router(service))
    return TestClient(app), manager


def test_list_instances(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/api/agent/instances")
    assert response.status_code == 200
    assert response.json()[0]["name"] == "oas1"


def test_unknown_instance_is_404(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/api/agent/nope/snapshot")
    assert response.status_code == 404


def test_snapshot(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/api/agent/oas1/snapshot")
    assert response.status_code == 200
    assert response.json()["name"] == "oas1"


def test_set_enabled_writes(tmp_path):
    client, manager = _client(tmp_path)
    response = client.post("/api/agent/oas1/tasks/Exploration/enable", json={"enabled": True})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert manager.config_cache("oas1").data["exploration"]["scheduler"]["enable"] is True


def test_protected_write_returns_403_with_token(tmp_path):
    client, _ = _client(tmp_path)
    response = client.post("/api/agent/oas1/tasks/MysteryShop/enable", json={"enabled": True})
    assert response.status_code == 403
    assert response.json()["detail"]["approval_token"]


def test_hold_blocks_write_with_423(tmp_path):
    client, _ = _client(tmp_path)
    client.post("/api/agent/hold", json={"minutes": 30, "reason": "playing"})
    response = client.post("/api/agent/oas1/tasks/Exploration/enable", json={"enabled": True})
    assert response.status_code == 423
    client.delete("/api/agent/hold")
    assert client.post("/api/agent/oas1/tasks/Exploration/enable", json={"enabled": True}).status_code == 200


def test_bad_payload_is_422(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/api/agent/oas1/tasks/Exploration/enable", json={}).status_code == 422


def test_next_run_validation_is_400(tmp_path):
    client, _ = _client(tmp_path)
    response = client.post("/api/agent/oas1/tasks/Exploration/next_run", json={"at": "tomorrow"})
    assert response.status_code == 400


def test_run_now_and_command(tmp_path):
    client, manager = _client(tmp_path)
    assert client.post("/api/agent/oas1/tasks/Exploration/run_now", json={"position": "front"}).status_code == 200
    assert client.post("/api/agent/oas1/command", json={"command": "goto_main"}).status_code == 200
    assert manager.script_process["oas1"].cmd_queue.get_nowait() == "goto_main"


def test_start_stop_instance(tmp_path):
    client, manager = _client(tmp_path)
    assert client.post("/api/agent/oas1/running", json={"running": False}).status_code == 200
    assert manager.script_process["oas1"].stopped == 1


def test_emergency_stop(tmp_path):
    client, manager = _client(tmp_path)
    response = client.post("/api/agent/emergency_stop", json={"minutes": 30})
    assert response.status_code == 200
    assert response.json()["stopped"] == ["oas1"]


def test_log_and_briefing_and_issues(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("/api/agent/oas1/log").status_code == 200
    assert client.get("/api/agent/briefing").status_code == 200
    assert client.post("/api/agent/issues", json={"task": "Orochi", "symptom": "x"}).status_code == 200
    assert client.get("/api/agent/issues").status_code == 200
