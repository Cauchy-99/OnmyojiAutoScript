import queue

from module.config.utils import convert_to_underscore


def default_tasks():
    """最小任务表：一个普通任务 + 一个受保护任务 + Restart + script 配置。"""
    scheduler = {
        "enable": True,
        "next_run": "2023-01-01 00:00:00",
        "priority": 5,
        "success_interval": "01 00:00:00",
        "failure_interval": "01 00:00:00",
    }
    return {
        "running_task": "",
        "script": {"optimization": {"schedule_rule": "Filter", "queue_mode": False}},
        "restart": {"scheduler": dict(scheduler)},
        "exploration": {"scheduler": dict(scheduler, enable=False)},
        "mystery_shop": {"scheduler": dict(scheduler, enable=False)},
    }


class FakeConfig:
    def __init__(self, name="oas1", tasks=None):
        self.name = name
        self.data = tasks if tasks is not None else default_tasks()
        self.model = _FakeModel(self)

    def script_task(self, task):
        task = convert_to_underscore(task)
        node = self.data.get(task, {})
        return {group: [] for group in node}

    def script_set_arg(self, task, group, argument, value):
        task = convert_to_underscore(task)
        group = convert_to_underscore(group)
        node = self.data.get(task, {}).get(group)
        if node is None:
            return False
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            value = value.strip().lower() == "true"
        node[argument] = value
        return True

    def task_call(self, task):
        task = convert_to_underscore(task)
        node = self.data.get(task, {}).get("scheduler")
        if node is None:
            return False
        node["next_run"] = "2026-10-23 18:15:00"
        return True

    def update_scheduler(self):
        self._pending = [
            {"name": key, "next_run": value["scheduler"]["next_run"]}
            for key, value in self.data.items()
            if isinstance(value, dict) and "scheduler" in value
            and value["scheduler"].get("enable")
        ]

    def get_schedule_data(self):
        return {"running": {}, "pending": getattr(self, "_pending", []), "waiting": []}


class _FakeModel:
    def __init__(self, config):
        self._config = config

    def dict(self):
        return self._config.data


class FakeProcess:
    def __init__(self, name="oas1", state="RUNNING"):
        self.name = name
        self.state = state
        self.alive = state == "RUNNING"
        self.cmd_queue = queue.Queue()
        self.frame_queue = queue.Queue(maxsize=1)
        self.started = 0
        self.stopped = 0

    def is_alive(self):
        return self.alive

    async def start(self):
        self.state = "RUNNING"
        self.alive = True
        self.started += 1

    async def stop(self):
        self.state = "INACTIVE"
        self.alive = False
        self.stopped += 1


class FakeManager:
    def __init__(self, names=("oas1",), configs=None):
        self.script_process = {name: FakeProcess(name) for name in names}
        self._configs = configs or {name: FakeConfig(name) for name in names}

    def config_cache(self, name):
        if name not in self._configs:
            raise FileNotFoundError(name)
        return self._configs[name]
