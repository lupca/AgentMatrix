import os
import sys
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure backend and frontend/chat are in sys.path
frontend_chat_dir = Path(__file__).resolve().parent.parent
backend_dir = frontend_chat_dir.parent.parent / "backend"
if str(frontend_chat_dir) not in sys.path:
    sys.path.insert(0, str(frontend_chat_dir))
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from router import route_message
from handlers import format_result, build_system_context, chat_with_context

import importlib.util
app_spec = importlib.util.spec_from_file_location("chat_app", frontend_chat_dir / "app.py")
chat_app = importlib.util.module_from_spec(app_spec)
app_spec.loader.exec_module(chat_app)
start = chat_app.start
main = chat_app.main



class TestRouter:
    def test_route_commands(self):
        assert route_message("/pm test task") == "pipeline"
        assert route_message("/lint") == "pipeline"
        assert route_message("/status") == "pipeline"

    def test_route_approval_keywords(self):
        assert route_message("approve") == "pipeline"
        assert route_message("REJECT") == "pipeline"
        assert route_message(" pass ") == "pipeline"
        assert route_message("changes") == "pipeline"
        assert route_message("Y") == "pipeline"
        assert route_message("n") == "pipeline"

    def test_route_questions(self):
        assert route_message("What is the status of task 1?") == "chat"
        assert route_message("How to execute spec gate?") == "chat"
        assert route_message("Why did it fail?") == "chat"

    def test_route_default_chat(self):
        assert route_message("Hello bot") == "chat"
        assert route_message("Please explain this code") == "chat"


class TestHandlers:
    def test_format_result_spec_awaiting(self):
        result = {
            "task_id": "CTV2-001",
            "title": "Test Task",
            "current_gate": "spec",
            "status": "todo",
            "awaiting_approval": True,
            "approval_prompt": "Approve task spec?",
        }
        output = format_result(result)
        assert "Created CTV2-001. Awaiting Spec Gate." in output
        assert "Approve task spec?" in output

    def test_format_result_error(self):
        result = {
            "task_id": "CTV2-002",
            "current_gate": "verdict",
            "error": "Four-eyes rule violation",
        }
        output = format_result(result)
        assert "Error at Gate `verdict`" in output
        assert "Four-eyes rule violation" in output

    def test_build_system_context(self):
        state = {
            "task_id": "CTV2-003",
            "title": "Build UI",
            "current_gate": "plan",
            "status": "todo",
            "mode": "supervised",
            "executor": "@antigravity",
        }
        context = build_system_context(state)
        assert "Task ID: CTV2-003" in context
        assert "Title: Build UI" in context
        assert "Current Gate: plan" in context

    @pytest.mark.asyncio
    async def test_chat_with_context_fallback(self):
        state = {"task_id": "CTV2-004", "title": "Test State"}
        with patch.dict(os.environ, {}, clear=True):
            response = await chat_with_context("What is the task ID?", state)
            assert "CTV2-004" in response
            assert "ANTHROPIC_API_KEY not configured" in response


import chainlit as cl

class TestChainlitIntegration:
    @pytest.mark.asyncio
    async def test_start(self):
        session_store = {}

        def mock_set(key, value):
            session_store[key] = value

        def mock_get(key):
            return session_store.get(key)

        mock_msg_cls = MagicMock()
        mock_msg_instance = MagicMock()
        mock_msg_instance.send = AsyncMock()
        mock_msg_cls.return_value = mock_msg_instance

        with patch.object(cl.user_session, "set", side_effect=mock_set), \
             patch.object(cl.user_session, "get", side_effect=mock_get), \
             patch("chainlit.Message", mock_msg_cls):

            await start()
            assert "graph" in session_store
            assert "thread_id" in session_store
            mock_msg_cls.assert_called_once()
            mock_msg_instance.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_pipeline_command(self):
        session_store = {}

        def mock_set(key, value):
            session_store[key] = value

        def mock_get(key):
            return session_store.get(key)

        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "task_id": "CTV2-005",
                "current_gate": "spec",
                "status": "todo",
                "awaiting_approval": True,
            }
        )
        session_store["graph"] = mock_graph
        session_store["thread_id"] = "test-thread-123"

        incoming_msg = MagicMock()
        incoming_msg.content = "/pm test task"

        mock_msg_cls = MagicMock()
        mock_msg_instance = MagicMock()
        mock_msg_instance.send = AsyncMock()
        mock_msg_cls.return_value = mock_msg_instance

        with patch.object(cl.user_session, "set", side_effect=mock_set), \
             patch.object(cl.user_session, "get", side_effect=mock_get), \
             patch("chainlit.Message", mock_msg_cls):

            await main(incoming_msg)
            mock_graph.ainvoke.assert_called_once()
            mock_msg_cls.assert_called_once()
            mock_msg_instance.send.assert_called_once()
