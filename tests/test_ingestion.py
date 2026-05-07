"""Tests for ingestion module."""
import pytest
from unittest.mock import patch, MagicMock
import hashlib


def test_compute_sha256():
    from ingestion import _compute_sha256
    data = b"hello world"
    expected = hashlib.sha256(data).hexdigest()
    assert _compute_sha256(data) == expected





def test_check_robots_txt_allows():
    from ingestion import _check_robots_txt
    # Most sites allow general crawling
    with patch("ingestion.RobotFileParser") as mock_rp:
        instance = MagicMock()
        instance.can_fetch.return_value = True
        mock_rp.return_value = instance
        assert _check_robots_txt("https://example.com/page") is True


def test_check_robots_txt_blocks():
    from ingestion import _check_robots_txt
    with patch("ingestion.RobotFileParser") as mock_rp:
        instance = MagicMock()
        instance.can_fetch.return_value = False
        mock_rp.return_value = instance
        assert _check_robots_txt("https://example.com/private") is False


def test_check_robots_txt_error_allows():
    from ingestion import _check_robots_txt
    with patch("ingestion.RobotFileParser") as mock_rp:
        mock_rp.return_value.read.side_effect = Exception("timeout")
        assert _check_robots_txt("https://unreachable.example.com") is True



