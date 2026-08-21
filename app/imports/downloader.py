"""Download and safely extract provider feed archives."""

from __future__ import annotations

import ipaddress
import logging
import shutil
import socket
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.db.models import Provider
from app.imports.exceptions import InvalidFeedArchiveError, TransientImportError

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 60.0
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024


def _validate_public_http_url(url: str) -> None:
    """Reject non-HTTP and non-public destinations before a request is made."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Provider feed_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Provider feed_url must not contain user credentials")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Provider feed_url must not target localhost")

    try:
        addresses = {ipaddress.ip_address(hostname)}
    except ValueError:
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = {
                ipaddress.ip_address(sockaddr[0])
                for _, _, _, _, sockaddr in socket.getaddrinfo(
                    hostname,
                    port,
                    type=socket.SOCK_STREAM,
                )
            }
        except (OSError, ValueError) as error:
            raise TransientImportError(
                f"Could not resolve provider feed host {hostname!r}: {error}"
            ) from error

    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError(
            "Provider feed_url resolves to a loopback, private, link-local, "
            "reserved, or otherwise non-public address"
        )


class _ValidatedRedirectHandler(HTTPRedirectHandler):
    """Apply the same SSRF checks to every HTTP redirect destination."""

    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Request | None:
        _validate_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(slots=True)
class DownloadedFeed:
    """Paths owned by one download and removed when its context exits."""

    zip_path: Path
    xml_path: Path
    _temporary_directory: Path

    def cleanup(self) -> None:
        """Remove the downloaded archive and extracted XML file."""
        shutil.rmtree(self._temporary_directory, ignore_errors=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.cleanup()


class DownloadService:
    """Download a provider ZIP feed and extract its XML snapshot."""

    def download(self, provider: Provider) -> DownloadedFeed:
        """Return managed paths for a downloaded ZIP and its extracted XML."""
        if not provider.feed_url:
            raise ValueError(f"Provider {provider.id} has no feed_url configured")

        timeout_seconds = self._timeout_seconds(provider)
        temporary_directory = Path(tempfile.mkdtemp(prefix="job-board-import-"))
        zip_path = temporary_directory / "feed.zip"
        xml_path = temporary_directory / "feed.xml"

        try:
            self._download_zip(provider.feed_url, zip_path, timeout_seconds)
            self._extract_xml(zip_path, xml_path)
            return DownloadedFeed(
                zip_path=zip_path,
                xml_path=xml_path,
                _temporary_directory=temporary_directory,
            )
        except Exception:
            shutil.rmtree(temporary_directory, ignore_errors=True)
            raise

    @staticmethod
    def _timeout_seconds(provider: Provider) -> float:
        config = provider.config or {}
        configured_timeout = config.get(
            "download_timeout_seconds",
            provider.timeout_seconds,
        )
        if configured_timeout is None:
            return _DEFAULT_TIMEOUT_SECONDS
        if isinstance(configured_timeout, bool) or not isinstance(
            configured_timeout, (int, float)
        ):
            raise TypeError("Provider download timeout must be numeric")
        if configured_timeout <= 0:
            raise ValueError("Provider download timeout must be greater than zero")
        return float(configured_timeout)

    @staticmethod
    def _download_zip(url: str, destination: Path, timeout_seconds: float) -> None:
        try:
            _validate_public_http_url(url)
            request = Request(
                url,
                headers={"User-Agent": "job-board-backend/0.1"},
            )
            opener = build_opener(_ValidatedRedirectHandler())
            with (
                opener.open(request, timeout=timeout_seconds) as response,
                destination.open("wb") as output,
            ):
                while chunk := response.read(_DOWNLOAD_CHUNK_SIZE):
                    output.write(chunk)
        except TransientImportError:
            raise
        except (HTTPError, URLError, TimeoutError, ConnectionError) as error:
            raise TransientImportError(
                f"Provider feed download failed: {error}"
            ) from error
        except OSError as error:
            raise TransientImportError(
                f"Provider feed download failed: {error}"
            ) from error

    @staticmethod
    def _extract_xml(zip_path: Path, destination: Path) -> None:
        try:
            with zipfile.ZipFile(zip_path) as archive:
                xml_members = sorted(
                    (
                        member
                        for member in archive.infolist()
                        if not member.is_dir()
                        and Path(member.filename).suffix.lower() == ".xml"
                    ),
                    key=lambda member: member.filename,
                )
                if not xml_members:
                    raise InvalidFeedArchiveError(
                        "Downloaded ZIP does not contain an XML feed"
                    )
                if len(xml_members) > 1:
                    raise InvalidFeedArchiveError(
                        "Downloaded ZIP contains more than one XML file"
                    )

                with (
                    archive.open(xml_members[0]) as source,
                    destination.open("wb") as output,
                ):
                    shutil.copyfileobj(source, output)
        except InvalidFeedArchiveError:
            raise
        except (zipfile.BadZipFile, RuntimeError, OSError) as error:
            raise InvalidFeedArchiveError(
                f"Downloaded feed is not a readable ZIP archive: {error}"
            ) from error
