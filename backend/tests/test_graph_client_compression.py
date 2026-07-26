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
