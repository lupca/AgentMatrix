import threading
import time
from unittest.mock import MagicMock, patch

import psutil
import pytest

from app.services.process_manager import ProcessManager, ProcessResult, ProcessStatus


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
