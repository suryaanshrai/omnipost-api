from django.core.management import call_command

from omnipost_api.management.commands import run_scheduler


def test_one_failing_task_does_not_stop_the_others(monkeypatch):
    ran = []

    def boom():
        raise RuntimeError("down")

    monkeypatch.setattr(
        run_scheduler,
        "_tasks",
        lambda: [run_scheduler.Task("a", 60, boom), run_scheduler.Task("b", 60, lambda: ran.append("b"))],
    )
    call_command("run_scheduler", "--once")
    assert ran == ["b"]


def test_only_selects_tasks(monkeypatch):
    ran = []
    monkeypatch.setattr(
        run_scheduler,
        "_tasks",
        lambda: [
            run_scheduler.Task("a", 60, lambda: ran.append("a")),
            run_scheduler.Task("b", 60, lambda: ran.append("b")),
        ],
    )
    call_command("run_scheduler", "--once", "--only", "b")
    assert ran == ["b"]
