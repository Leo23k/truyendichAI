from app.worker_control import WorkerProcessManager


def test_start_does_not_duplicate_an_external_worker(monkeypatch) -> None:
    manager = WorkerProcessManager()
    monkeypatch.setattr(manager, "_external_running", lambda _name: True)
    ok, message = manager.start("crawler")
    assert ok is True
    assert "không tạo process trùng" in message
    assert manager._processes["crawler"] is None


def test_external_worker_cannot_be_stopped_or_restarted(monkeypatch) -> None:
    manager = WorkerProcessManager()
    monkeypatch.setattr(manager, "_external_running", lambda _name: True)
    assert manager.stop("crawler")[0] is False
    assert manager.restart("crawler")[0] is False
