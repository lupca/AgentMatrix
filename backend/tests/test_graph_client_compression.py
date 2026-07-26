"""Tests for Headroom compression integration with MCP responses."""
import json
import pytest
from unittest.mock import patch, MagicMock

from app.core.compression import (
    compress_for_prompt,
    compress_file_list,
    compress_test_list,
    compress_flow_list,
)


class TestCompressionDisabled:
    """Tests when compression is disabled (default)."""

    def test_short_data_returned_unchanged(self):
        """Short data should be returned as-is."""
        data = ["file1.py", "file2.py"]
        result = compress_for_prompt(data)
        assert result == json.dumps(data, ensure_ascii=False)

    def test_compress_file_list_empty(self):
        """Empty file list returns 'None'."""
        assert compress_file_list([]) == "None"

    def test_compress_test_list_empty(self):
        """Empty test list returns 'None'."""
        assert compress_test_list([]) == "None"

    def test_compress_flow_list_empty(self):
        """Empty flow list returns 'None'."""
        assert compress_flow_list([]) == "None"


class TestCompressionEnabled:
    """Tests when compression is enabled."""

    @pytest.fixture
    def enable_compression(self):
        """Enable compression for tests."""
        with patch("app.core.compression.settings") as mock_settings:
            mock_settings.HEADROOM_COMPRESSION_ENABLED = True
            mock_settings.HEADROOM_MIN_CHARS = 100
            yield mock_settings

    def test_short_data_bypasses_compression(self, enable_compression):
        """Data shorter than min_chars should bypass compression."""
        data = ["a.py", "b.py"]
        result = compress_for_prompt(data)
        # Should be original JSON since it's short
        assert result == json.dumps(data, ensure_ascii=False)

    def test_large_data_compressed(self, enable_compression):
        """Large data should be compressed when headroom is available."""
        large_data = [f"path/to/file_{i}.py" for i in range(100)]
        serialized = json.dumps(large_data, ensure_ascii=False)

        # Mock headroom
        mock_result = MagicMock()
        mock_result.messages = [{"content": "[compressed: 100 files]"}]

        with patch.dict("sys.modules", {"headroom": MagicMock()}):
            import sys
            sys.modules["headroom"].compress.return_value = mock_result

            result = compress_for_prompt(large_data)
            # Should call headroom.compress for large data
            assert len(result) < len(serialized) or result == "[compressed: 100 files]"


class TestDataIntegrity:
    """Tests to ensure compressed output preserves critical information."""

    def test_file_paths_preserved_in_short_list(self):
        """File paths should be preserved when list is short (no compression)."""
        files = [
            "backend/app/services/graph_client.py",
            "backend/app/core/config.py",
            "backend/tests/test_compression.py",
        ]
        result = compress_file_list(files)
        parsed = json.loads(result)

        for f in files:
            assert f in parsed

    def test_test_names_preserved_in_short_list(self):
        """Test names should be preserved when list is short."""
        tests = [
            "tests/test_graph_client.py",
            "tests/test_mcp.py",
        ]
        result = compress_test_list(tests)
        parsed = json.loads(result)

        for t in tests:
            assert t in parsed

    def test_flow_names_preserved_in_short_list(self):
        """Flow names should be preserved when list is short."""
        flows = ["user-login", "task-create", "dispatch-executor"]
        result = compress_flow_list(flows)
        parsed = json.loads(result)

        for f in flows:
            assert f in parsed


class TestCompressionRatio:
    """Tests for compression ratio on large data."""

    @pytest.fixture
    def mock_headroom_compression(self):
        """Mock headroom to return compressed data."""
        def compress_mock(messages, compress_user_messages=False):
            original = messages[0]["content"]
            # Simulate ~60% compression
            compressed_len = int(len(original) * 0.4)
            compressed = original[:compressed_len] + "..."
            result = MagicMock()
            result.messages = [{"content": compressed}]
            return result

        with patch("app.core.compression.settings") as mock_settings:
            mock_settings.HEADROOM_COMPRESSION_ENABLED = True
            mock_settings.HEADROOM_MIN_CHARS = 100

            with patch.dict("sys.modules", {"headroom": MagicMock()}):
                import sys
                sys.modules["headroom"].compress = compress_mock
                yield

    def test_large_list_compression_ratio(self, mock_headroom_compression):
        """Large list should achieve significant compression ratio."""
        large_files = [f"very/long/path/to/module_{i}/file_{i}.py" for i in range(100)]
        original = json.dumps(large_files, ensure_ascii=False)
        result = compress_for_prompt(large_files)

        # Should be significantly smaller than original
        ratio = len(result) / len(original)
        assert ratio < 0.6, f"Expected >40% compression, got {(1-ratio)*100:.1f}%"


class TestErrorHandling:
    """Tests for error handling."""

    def test_headroom_import_error_fallback(self):
        """Should fallback to original when headroom not installed."""
        with patch("app.core.compression.settings") as mock_settings:
            mock_settings.HEADROOM_COMPRESSION_ENABLED = True
            mock_settings.HEADROOM_MIN_CHARS = 10

            # Force ImportError
            with patch.dict("sys.modules", {"headroom": None}):
                data = ["file1.py", "file2.py", "file3.py"]
                result = compress_for_prompt(data)
                # Should return original JSON
                assert result == json.dumps(data, ensure_ascii=False)

    def test_headroom_exception_fallback(self):
        """Should fallback to original when headroom raises exception."""
        with patch("app.core.compression.settings") as mock_settings:
            mock_settings.HEADROOM_COMPRESSION_ENABLED = True
            mock_settings.HEADROOM_MIN_CHARS = 10

            mock_headroom = MagicMock()
            mock_headroom.compress.side_effect = RuntimeError("Compression failed")

            with patch.dict("sys.modules", {"headroom": mock_headroom}):
                data = ["file1.py", "file2.py", "file3.py"]
                result = compress_for_prompt(data)
                # Should return original JSON on error
                assert result == json.dumps(data, ensure_ascii=False)


from unittest.mock import patch, MagicMock, AsyncMock


class TestGraphClientIntegration:
    """Integration tests for graph_client compression."""

    @pytest.mark.asyncio
    async def test_get_impact_radius_compress_output(self):
        """Test compress_output parameter in get_impact_radius."""
        from app.services.graph_client import get_impact_radius

        # Mock MCPClient
        with patch("app.services.graph_client.MCPClient") as MockMCP:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.call_tool = AsyncMock(return_value={
                "impacted_files": ["file1.py", "file2.py", "file3.py"]
            })
            MockMCP.return_value = mock_client

            # Without compression - returns list
            result = await get_impact_radius("/repo", "main.py", use_cache=False)
            assert isinstance(result, list)
            assert "file1.py" in result

            # With compression - returns string
            result = await get_impact_radius("/repo", "main.py", use_cache=False, compress_output=True)
            assert isinstance(result, str)
            assert "file1.py" in result

    @pytest.mark.asyncio
    async def test_query_tests_for_compress_output(self):
        """Test compress_output parameter in query_tests_for."""
        from app.services.graph_client import query_tests_for

        with patch("app.services.graph_client.MCPClient") as MockMCP:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.call_tool = AsyncMock(return_value={
                "results": [{"file_path": "test_main.py"}, {"file_path": "test_utils.py"}]
            })
            MockMCP.return_value = mock_client

            # Without compression
            result = await query_tests_for("/repo", "main.py", use_cache=False)
            assert isinstance(result, list)

            # With compression
            result = await query_tests_for("/repo", "main.py", use_cache=False, compress_output=True)
            assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_get_affected_flows_compress_output(self):
        """Test compress_output parameter in get_affected_flows."""
        from app.services.graph_client import get_affected_flows

        with patch("app.services.graph_client.MCPClient") as MockMCP:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.call_tool = AsyncMock(return_value={
                "affected_flows": [{"name": "user-login"}, {"name": "task-create"}]
            })
            MockMCP.return_value = mock_client

            # Without compression
            result = await get_affected_flows("/repo", ["main.py"], use_cache=False)
            assert isinstance(result, list)

            # With compression
            result = await get_affected_flows("/repo", ["main.py"], use_cache=False, compress_output=True)
            assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_full_gate_flow_with_compression(self):
        """Integration test: full gate flow with compression enabled."""
        from app.services.graph_client import get_impact_radius, query_tests_for, get_affected_flows

        with patch("app.services.graph_client.MCPClient") as MockMCP:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            # Simulate large MCP responses
            large_files = [f"path/to/module_{i}/file_{i}.py" for i in range(50)]
            large_tests = [{"file_path": f"tests/test_{i}.py"} for i in range(30)]
            large_flows = [{"name": f"flow-{i}"} for i in range(20)]

            mock_client.call_tool = AsyncMock(side_effect=[
                {"impacted_files": large_files},
                {"results": large_tests},
                {"affected_flows": large_flows},
            ])
            MockMCP.return_value = mock_client

            # Simulate gate flow: fetch all data with compression
            files = await get_impact_radius("/repo", "main.py", use_cache=False, compress_output=True)
            tests = await query_tests_for("/repo", "main.py", use_cache=False, compress_output=True)
            flows = await get_affected_flows("/repo", ["main.py"], use_cache=False, compress_output=True)

            # All results should be compressed strings
            assert isinstance(files, str)
            assert isinstance(tests, str)
            assert isinstance(flows, str)

            # Data should still be present in compressed output
            assert "module_0" in files
            assert "test_0" in tests
            assert "flow-0" in flows
