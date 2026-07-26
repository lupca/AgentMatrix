from concurrent.futures import ThreadPoolExecutor

from app.services.process_manager import ProcessManager, ProcessResult, ProcessStatus


def execute_agent(index):
    results = list(
        ProcessManager(timeout_seconds=5).run_with_streaming(
            f"printf 'agent-{index}\\n'",
            "/tmp",
        )
    )
    output = [item for item in results if isinstance(item, str)]
    final = next(item for item in results if isinstance(item, ProcessResult))
    return output, final


def test_10_concurrent_agents_are_isolated():
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(execute_agent, range(10)))

    for index, (output, final) in enumerate(results):
        assert output == [f"agent-{index}"]
        assert final.status == ProcessStatus.COMPLETED
        assert final.exit_code == 0
