"""Tests for release parsing, trust boundaries, and update preferences."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from daq_xy_qt_readback.update_checker import (
    UpdatePreferences,
    fetch_latest_release,
    is_newer_release,
    load_update_preferences,
    parse_release_payload,
    save_update_preferences,
)


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class UpdateCheckerTests(unittest.TestCase):
    def test_release_payload_accepts_only_this_repository(self) -> None:
        release = parse_release_payload(
            {
                "tag_name": "v1.2.3",
                "name": "Version 1.2.3",
                "html_url": "https://github.com/ylylyl98/daq_xy_qt_readback_repo/releases/tag/v1.2.3",
                "body": "Safety improvements",
                "draft": False,
            }
        )
        self.assertEqual(release.version, "1.2.3")
        self.assertEqual(release.notes, "Safety improvements")

        with self.assertRaisesRegex(ValueError, "untrusted"):
            parse_release_payload(
                {
                    "tag_name": "v9.9.9",
                    "html_url": "https://example.com/malicious-installer.exe",
                }
            )

    def test_fetch_uses_injected_transport(self) -> None:
        payload = {
            "tag_name": "v1.1.0",
            "html_url": "https://github.com/ylylyl98/daq_xy_qt_readback_repo/releases/tag/v1.1.0",
        }
        calls: list[float] = []

        def opener(_request: object, *, timeout: float) -> _FakeResponse:
            calls.append(timeout)
            return _FakeResponse(payload)

        release = fetch_latest_release(timeout=2.5, opener=opener)
        self.assertEqual(release.version, "1.1.0")
        self.assertEqual(calls, [2.5])

    def test_semantic_version_comparison(self) -> None:
        self.assertTrue(is_newer_release("1.0.9", "1.1.0"))
        self.assertFalse(is_newer_release("1.1.0", "1.1.0"))
        self.assertFalse(is_newer_release("2.0.0", "1.9.9"))

    def test_preferences_limit_automatic_checks_and_round_trip(self) -> None:
        now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        preferences = UpdatePreferences(skipped_version="1.2.0")
        preferences.mark_checked(now)
        self.assertTrue(preferences.checked_recently(now + timedelta(hours=23)))
        self.assertFalse(preferences.checked_recently(now + timedelta(hours=25)))

        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "update_settings.json"
            save_update_preferences(path, preferences)
            self.assertEqual(load_update_preferences(path), preferences)


if __name__ == "__main__":
    unittest.main()
