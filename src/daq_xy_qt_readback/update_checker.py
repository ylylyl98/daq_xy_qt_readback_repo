"""Safe, hardware-independent GitHub release update checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from packaging.version import InvalidVersion, Version


GITHUB_REPOSITORY = "ylylyl98/daq_xy_qt_readback_repo"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
TRUSTED_RELEASE_HOSTS = {"github.com", "www.github.com"}
DEFAULT_CHECK_INTERVAL = timedelta(hours=24)


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    name: str
    page_url: str
    notes: str = ""


@dataclass
class UpdatePreferences:
    last_checked_utc: str = ""
    skipped_version: str = ""

    def checked_recently(
        self,
        now: datetime | None = None,
        interval: timedelta = DEFAULT_CHECK_INTERVAL,
    ) -> bool:
        if not self.last_checked_utc:
            return False
        try:
            checked = datetime.fromisoformat(self.last_checked_utc.replace("Z", "+00:00"))
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        now = now or datetime.now(timezone.utc)
        return now - checked < interval

    def mark_checked(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.last_checked_utc = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_update_preferences(path: Path) -> UpdatePreferences:
    if not path.exists():
        return UpdatePreferences()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return UpdatePreferences()
        return UpdatePreferences(
            last_checked_utc=str(payload.get("last_checked_utc", "")),
            skipped_version=str(payload.get("skipped_version", "")),
        )
    except Exception:
        return UpdatePreferences()


def save_update_preferences(path: Path, preferences: UpdatePreferences) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(preferences), indent=2), encoding="utf-8")


def _trusted_release_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in TRUSTED_RELEASE_HOSTS:
        raise ValueError("The update response contained an untrusted release URL.")
    expected_prefix = f"/{GITHUB_REPOSITORY}/releases/"
    if not parsed.path.startswith(expected_prefix):
        raise ValueError("The update response did not point to this application's releases.")
    return url


def parse_release_payload(payload: dict[str, Any]) -> ReleaseInfo:
    if payload.get("draft"):
        raise ValueError("The latest GitHub release is still a draft.")
    tag = str(payload.get("tag_name", "")).strip()
    version = tag[1:] if tag.lower().startswith("v") else tag
    try:
        Version(version)
    except InvalidVersion as exc:
        raise ValueError(f"Invalid release version: {tag or '<missing>'}") from exc
    return ReleaseInfo(
        version=version,
        name=str(payload.get("name") or tag or f"Version {version}"),
        page_url=_trusted_release_url(payload.get("html_url")),
        notes=str(payload.get("body") or "").strip(),
    )


def fetch_latest_release(
    *,
    timeout: float = 4.0,
    opener: Callable[..., Any] = urlopen,
) -> ReleaseInfo:
    request = Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "DAQ-XY-Control-update-checker",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with opener(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Unexpected GitHub update response.")
    return parse_release_payload(payload)


def is_newer_release(current_version: str, release_version: str) -> bool:
    try:
        return Version(release_version) > Version(current_version)
    except InvalidVersion as exc:
        raise ValueError("Unable to compare application versions.") from exc
