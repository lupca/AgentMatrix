import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

from app.services.process_manager import (
    ProcessManager,
    ProcessResult,
    ProcessStatus,
    WorktreeManager,
    WorktreeUnsupportedError,
)


def final_result(results):
    return next(item for item in results if isinstance(item, ProcessResult))


def test_rejects_non_positive_timeout():
    with pytest.raises(ValueError, match="greater than zero"):
        ProcessManager(timeout_seconds=0)


def test_successful_execution():
    results = list(ProcessManager(timeout_seconds=5).run_with_streaming("echo hello", "/tmp"))

    assert [item for item in results if isinstance(item, str)] == ["hello"]
    assert final_result(results) == ProcessResult(ProcessStatus.COMPLETED, 0, None)


def test_merges_custom_environment():
    results = list(
        ProcessManager(timeout_seconds=5).run_with_streaming(
            "printf \"$CTV2_PROCESS_TEST\"",
            "/tmp",
            env={"CTV2_PROCESS_TEST": 123},
        )
    )

    assert [item for item in results if isinstance(item, str)] == ["123"]


def test_captures_merged_output_lines():
    results = list(
        ProcessManager(timeout_seconds=5).run_with_streaming(
            "printf 'one\\ntwo\\n'; printf 'three\\n' >&2",
            "/tmp",
        )
    )

    assert [item for item in results if isinstance(item, str)] == ["one", "two", "three"]


def test_timeout_terminates_process():
    started = time.monotonic()
    results = list(
        ProcessManager(timeout_seconds=1, terminate_grace_seconds=0.2).run_with_streaming(
            "sleep 60",
            "/tmp",
        )
    )

    assert time.monotonic() - started < 3
    assert final_result(results).status == ProcessStatus.TIMEOUT
    assert final_result(results).exit_code == -1


def test_failed_command_returns_exit_code():
    results = list(ProcessManager(timeout_seconds=5).run_with_streaming("exit 42", "/tmp"))

    assert final_result(results).status == ProcessStatus.FAILED
    assert final_result(results).exit_code == 42


def test_cancellation_terminates_process():
    manager = ProcessManager(timeout_seconds=30, terminate_grace_seconds=0.2)
    results = []
    runner = threading.Thread(
        target=lambda: results.extend(manager.run_with_streaming("sleep 60", "/tmp"))
    )
    runner.start()
    deadline = time.monotonic() + 2
    while manager.pid is None and time.monotonic() < deadline:
        time.sleep(0.01)

    manager.cancel()
    runner.join(timeout=3)

    assert not runner.is_alive()
    assert final_result(results).status == ProcessStatus.CANCELLED


def test_cancel_check_terminates_process():
    manager = ProcessManager(timeout_seconds=5, cancel_check=lambda: True)

    results = list(manager.run_with_streaming("sleep 60", "/tmp"))

    assert final_result(results).status == ProcessStatus.CANCELLED


def test_cancel_check_error_is_treated_as_not_cancelled():
    manager = ProcessManager(
        timeout_seconds=2,
        cancel_check=lambda: (_ for _ in ()).throw(RuntimeError("redis down")),
    )

    assert manager._is_cancelled() is False


def test_local_cancel_flag_is_detected_without_process():
    manager = ProcessManager(timeout_seconds=2)

    manager.cancel()

    assert manager._is_cancelled() is True


def test_stdin_is_devnull():
    results = list(ProcessManager(timeout_seconds=2).run_with_streaming("cat", "/tmp"))

    assert final_result(results).status == ProcessStatus.COMPLETED


def test_on_start_receives_pid_before_silent_process_completes():
    started_pids = []
    manager = ProcessManager(timeout_seconds=2, on_start=started_pids.append)

    results = list(manager.run_with_streaming("sleep 0.1", "/tmp"))

    assert started_pids == [manager.pid]
    assert final_result(results).status == ProcessStatus.COMPLETED


def test_timeout_kills_descendant_processes():
    results = list(
        ProcessManager(timeout_seconds=1, terminate_grace_seconds=0.2).run_with_streaming(
            "bash -c 'sleep 60 & echo $!; sleep 60'",
            "/tmp",
        )
    )
    child_pid = int(next(item for item in results if isinstance(item, str)))

    assert final_result(results).status == ProcessStatus.TIMEOUT
    assert not psutil.pid_exists(child_pid)


def test_missing_working_directory_is_a_clean_failure():
    results = list(
        ProcessManager(timeout_seconds=2).run_with_streaming(
            "echo unreachable",
            "/definitely/not/a/real/directory",
        )
    )

    assert final_result(results).status == ProcessStatus.FAILED
    assert "Working directory" in final_result(results).error


def test_spawn_error_is_a_clean_failure():
    with patch(
        "app.services.process_manager.subprocess.Popen",
        side_effect=OSError("cannot spawn"),
    ):
        results = list(
            ProcessManager(timeout_seconds=2).run_with_streaming("echo test", "/tmp")
        )

    assert final_result(results).status == ProcessStatus.FAILED
    assert final_result(results).error == "cannot spawn"


def test_closing_generator_terminates_process():
    manager = ProcessManager(timeout_seconds=30, terminate_grace_seconds=0.2)
    stream = manager.run_with_streaming("echo started; sleep 60", "/tmp")

    assert next(stream) == "started"
    pid = manager.pid
    stream.close()

    assert pid is not None
    assert not psutil.pid_exists(pid)


def test_terminate_force_kills_processes_that_ignore_sigterm():
    manager = ProcessManager(timeout_seconds=2)
    process = MagicMock(pid=12345)
    parent = MagicMock()
    survivor = MagicMock()
    parent.children.return_value = [survivor]
    manager.process = process

    with (
        patch("app.services.process_manager.psutil.Process", return_value=parent),
        patch("app.services.process_manager.os.killpg"),
        patch(
            "app.services.process_manager.psutil.wait_procs",
            side_effect=[([], [survivor]), ([survivor], [])],
        ) as wait_procs,
    ):
        manager.terminate()

    survivor.kill.assert_called_once()
    assert wait_procs.call_count == 2
    process.wait.assert_called_once()


def test_terminate_falls_back_when_process_group_signal_is_denied():
    manager = ProcessManager(timeout_seconds=2)
    process = MagicMock(pid=12345)
    parent = MagicMock()
    parent.children.return_value = []
    manager.process = process

    with (
        patch("app.services.process_manager.psutil.Process", return_value=parent),
        patch(
            "app.services.process_manager.os.killpg",
            side_effect=PermissionError,
        ),
        patch(
            "app.services.process_manager.psutil.wait_procs",
            return_value=([], []),
        ),
    ):
        manager.terminate()

    parent.terminate.assert_called_once()


def _head(repo_root: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _worktree_list(repo_root: str) -> str:
    return subprocess.run(
        ["git", "worktree", "list"], cwd=repo_root, check=True, capture_output=True, text=True
    ).stdout


def _commit_in(worktree_path: str, message: str) -> str:
    (Path(worktree_path) / f"{message}.txt").write_text(message)
    subprocess.run(["git", "add", "."], cwd=worktree_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=worktree_path, check=True, capture_output=True
    )
    return _head(worktree_path)


def test_worktree_create_checks_out_isolated_copy(git_repo_root, tmp_path):
    manager = WorktreeManager(git_repo_root, worktree_root=str(tmp_path / "worktrees"))
    base = _head(git_repo_root)

    path = manager.create("run-a", base)

    assert Path(path).is_dir()
    assert _head(path) == base
    assert "run-a" in _worktree_list(git_repo_root)


def test_worktree_commits_are_visible_in_main_repo_after_removal(git_repo_root, tmp_path):
    manager = WorktreeManager(git_repo_root, worktree_root=str(tmp_path / "worktrees"))
    base = _head(git_repo_root)
    path = manager.create("run-b", base)

    commit_sha = _commit_in(path, "change-from-worktree")

    # No merge performed -- the commit is already reachable from the shared
    # object store, and stays reachable once the worktree checkout is gone.
    manager.remove("run-b")

    assert "run-b" not in _worktree_list(git_repo_root)
    assert not Path(path).exists()
    log = subprocess.run(
        ["git", "log", "--all", "--format=%H"],
        cwd=git_repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert commit_sha in log


def test_worktree_create_raises_for_non_git_directory(tmp_path):
    manager = WorktreeManager(str(tmp_path), worktree_root=str(tmp_path / "worktrees"))

    with pytest.raises(WorktreeUnsupportedError):
        manager.create("run-c", "HEAD")


def test_worktree_remove_is_idempotent_for_missing_worktree(git_repo_root, tmp_path):
    manager = WorktreeManager(git_repo_root, worktree_root=str(tmp_path / "worktrees"))

    manager.remove("never-created")  # must not raise


def test_worktree_create_recovers_from_a_stale_orphan(git_repo_root, tmp_path):
    manager = WorktreeManager(git_repo_root, worktree_root=str(tmp_path / "worktrees"))
    base = _head(git_repo_root)
    first_path = manager.create("run-d", base)
    _commit_in(first_path, "orphaned")

    # Simulate a crash: nothing calls remove(), so the worktree/branch are
    # still registered when the same run_id is retried.
    second_path = manager.create("run-d", base)

    assert second_path == first_path
    assert _head(second_path) == base


def test_two_concurrent_worktrees_commit_independently_without_lock_contention(
    git_repo_root, tmp_path
):
    manager = WorktreeManager(git_repo_root, worktree_root=str(tmp_path / "worktrees"))
    base = _head(git_repo_root)
    path_a = manager.create("run-e", base)
    path_b = manager.create("run-f", base)

    shas = {}
    errors = []

    def commit(run_id, path, label):
        try:
            shas[run_id] = _commit_in(path, label)
        except Exception as exc:  # pragma: no cover - failure surfaced via assert
            errors.append(exc)

    t1 = threading.Thread(target=commit, args=("run-e", path_a, "from-e"))
    t2 = threading.Thread(target=commit, args=("run-f", path_b, "from-f"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors
    assert not Path(git_repo_root, ".git", "index.lock").exists()
    assert shas["run-e"] != shas["run-f"]

    manager.remove("run-e")
    manager.remove("run-f")

    log = subprocess.run(
        ["git", "log", "--all", "--format=%H"],
        cwd=git_repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert shas["run-e"] in log
    assert shas["run-f"] in log
    assert _worktree_list(git_repo_root).count("[") == 1  # only the primary checkout remains
