import json

from client.state.manager import StateManager
from client.state.models import TaskStep
from client.state.session_events import SessionEventStore


def test_session_task_round_trip(tmp_path):
    state = StateManager(tmp_path)
    state.set_task(objective="Inspect particles", status="active",
                   current_step=0, steps=[TaskStep(label="Acquire overview")],
                   status_summary="Preparing an overview.")
    loaded = StateManager(tmp_path)
    assert loaded.task.objective == "Inspect particles"
    assert loaded.task.steps[0].label == "Acquire overview"


def test_session_event_store_append_and_limit(tmp_path):
    store = SessionEventStore(tmp_path)
    store.append("reasoning_summary", actor="agent", summary="Finding a target")
    store.append("tool_call", actor="operator", summary="Blank beam",
                 data={"command": "set_beam_blank"})
    assert [e["actor"] for e in store.list(1)] == ["operator"]
    lines = (tmp_path / "session_events.jsonl").read_text().splitlines()
    assert all(isinstance(json.loads(line), dict) for line in lines)
