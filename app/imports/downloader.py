"""Download and safely extract provider feed archives."""

from __future__ import annotations

import ipaddress
import logging
import shutil
import socket
import tempfile
import time
import zipfile
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from app.db.models import Provider
from app.imports.exceptions import InvalidFeedArchiveError, TransientImportError

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 60.0
_DEFAULT_RETRY_BACKOFF_SECONDS = 1.0
_DEFAULT_RETRY_BACKOFF_MAX_SECONDS = 30.0
_DEFAULT_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
_DEFAULT_MAX_EXTRACTED_BYTES = 16 * 1024 * 1024 * 1024
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_RESOLVED_TARGET_ATTRIBUTE = "_job_board_resolved_target"


@dataclass(frozen=True, slots=True)
class _ResolvedTarget:
    """A public destination whose DNS answer is pinned for one request."""

    url: str
    hostname: str
    port: int
    address: ipaddress.IPv4Address | ipaddress.IPv6Address


def _resolve_public_http_url(url: str) -> _ResolvedTarget:
    """Resolve once and return one validated public address for a request."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Provider feed_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Provider feed_url must not contain user credentials")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Provider feed_url must not target localhost")

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        raise ValueError("Provider feed_url contains an invalid port") from error

    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            hostname = hostname.encode("idna").decode("ascii")
            addresses = list(
                dict.fromkeys(
                    ipaddress.ip_address(sockaddr[0])
                    for _, _, _, _, sockaddr in socket.getaddrinfo(
                        hostname,
                        port,
                        type=socket.SOCK_STREAM,
                    )
                )
            )
        except (OSError, ValueError) as error:
            raise TransientImportError(
                f"Could not resolve provider feed host {hostname!r}: {error}"
            ) from error

    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError(
            "Provider feed_url resolves to a loopback, private, link-local, "
            "reserved, or otherwise non-public address"
        )

    return _ResolvedTarget(
        url=url,
        hostname=hostname,
        port=port,
        address=addresses[0],
    )


def _validate_public_http_url(url: str) -> None:
    """Reject non-HTTP and non-public destinations before a request is made."""
    _resolve_public_http_url(url)


class _ResolvedAddressConnectionMixin:
    """Connect to a pinned IP while retaining the origin hostname."""

    def __init__(self, *args: Any, resolved_address: str, **kwargs: Any) -> None:
        self._resolved_address = resolved_address
        super().__init__(*args, **kwargs)

    def connect(self) -> None:
        original_create_connection = self._create_connection

        def create_connection(
            address: tuple[str, int],
            timeout: object = socket._GLOBAL_DEFAULT_TIMEOUT,
            source_address: tuple[str, int] | None = None,
        ) -> socket.socket:
            return original_create_connection(
                (self._resolved_address, address[1]),
                timeout,
                source_address,
            )

        self._create_connection = create_connection
        try:
            super().connect()
        finally:
            self._create_connection = original_create_connection


class _ResolvedHTTPConnection(_ResolvedAddressConnectionMixin, HTTPConnection):
    """HTTP connection pinned to a previously validated address."""


class _ResolvedHTTPSConnection(_ResolvedAddressConnectionMixin, HTTPSConnection):
    """HTTPS connection pinned to an address with hostname SNI preserved."""


def _target_for_request(request: Request) -> _ResolvedTarget:
    target = getattr(request, _RESOLVED_TARGET_ATTRIBUTE, None)
    if isinstance(target, _ResolvedTarget) and target.url == request.full_url:
        return target
    return _resolve_public_http_url(request.full_url)


class _ResolvedHTTPHandler(HTTPHandler):
    """Open HTTP requests against their pinned public address."""

    def http_open(self, req: Request) -> object:
        target = _target_for_request(req)

        def connection_factory(
            _host: str,
            timeout: object = socket._GLOBAL_DEFAULT_TIMEOUT,
            **kwargs: Any,
        ) -> _ResolvedHTTPConnection:
            return _ResolvedHTTPConnection(
                target.hostname,
                port=target.port,
                timeout=timeout,
                resolved_address=str(target.address),
                **kwargs,
            )

        return self.do_open(connection_factory, req)


class _ResolvedHTTPSHandler(HTTPSHandler):
    """Open HTTPS requests against a pinned address with origin-host SNI."""

    def https_open(self, req: Request) -> object:
        target = _target_for_request(req)

        def connection_factory(
            _host: str,
            timeout: object = socket._GLOBAL_DEFAULT_TIMEOUT,
            **kwargs: Any,
        ) -> _ResolvedHTTPSConnection:
            return _ResolvedHTTPSConnection(
                target.hostname,
                port=target.port,
                timeout=timeout,
                resolved_address=str(target.address),
                **kwargs,
            )

        return self.do_open(
            connection_factory,
            req,
            context=self._context,
            check_hostname=self._check_hostname,
        )


class _ValidatedRedirectHandler(HTTPRedirectHandler):
    """Resolve, validate, and pin every HTTP redirect destination."""

    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Request | None:
        target = _resolve_public_http_url(newurl)
        redirected_request = super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            newurl,
        )
        if redirected_request is not None:
            setattr(
                redirected_request,
                _RESOLVED_TARGET_ATTRIBUTE,
                target,
            )
        return redirected_request


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
        retry_max_attempts = self._retry_max_attempts(provider)
        retry_backoff_seconds = self._configured_non_negative_number(
            provider,
            "download_retry_backoff_seconds",
            _DEFAULT_RETRY_BACKOFF_SECONDS,
        )
        retry_backoff_max_seconds = self._configured_non_negative_number(
            provider,
            "download_retry_backoff_max_seconds",
            _DEFAULT_RETRY_BACKOFF_MAX_SECONDS,
        )
        max_download_bytes = self._configured_positive_integer(
            provider,
            "download_max_bytes",
            _DEFAULT_MAX_DOWNLOAD_BYTES,
        )
        max_extracted_bytes = self._configured_positive_integer(
            provider,
            "download_max_extracted_bytes",
            _DEFAULT_MAX_EXTRACTED_BYTES,
        )
        temporary_directory = Path(tempfile.mkdtemp(prefix="job-board-import-"))
        zip_path = temporary_directory / "feed.zip"
        xml_path = temporary_directory / "feed.xml"

        try:
            self._download_with_retries(
                provider.feed_url,
                zip_path,
                timeout_seconds,
                max_download_bytes,
                retry_max_attempts,
                retry_backoff_seconds,
                retry_backoff_max_seconds,
            )
            self._extract_xml(zip_path, xml_path, max_extracted_bytes)
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
    def _retry_max_attempts(provider: Provider) -> int:
        retry_max_attempts = provider.retry_max_attempts
        if isinstance(retry_max_attempts, bool) or not isinstance(
            retry_max_attempts,
            int,
        ):
            raise TypeError("Provider retry_max_attempts must be an integer")
        if retry_max_attempts <= 0:
            raise ValueError("Provider retry_max_attempts must be greater than zero")
        return retry_max_attempts

    @staticmethod
    def _configured_non_negative_number(
        provider: Provider,
        key: str,
        default: float,
    ) -> float:
        configured_value = (provider.config or {}).get(key, default)
        if isinstance(configured_value, bool) or not isinstance(
            configured_value,
            (int, float),
        ):
            raise TypeError(f"Provider {key} must be numeric")
        if configured_value < 0:
            raise ValueError(f"Provider {key} must not be negative")
        return float(configured_value)

    @staticmethod
    def _configured_positive_integer(
        provider: Provider,
        key: str,
        default: int,
    ) -> int:
        configured_value = (provider.config or {}).get(key, default)
        if isinstance(configured_value, bool) or not isinstance(
            configured_value,
            int,
        ):
            raise TypeError(f"Provider {key} must be an integer")
        if configured_value <= 0:
            raise ValueError(f"Provider {key} must be greater than zero")
        return configured_value

    def _download_with_retries(
        self,
        url: str,
        destination: Path,
        timeout_seconds: float,
        max_download_bytes: int,
        max_attempts: int,
        backoff_seconds: float,
        backoff_max_seconds: float,
    ) -> None:
        for attempt in range(1, max_attempts + 1):
            try:
                self._download_zip(
                    url,
                    destination,
                    timeout_seconds,
                    max_download_bytes,
                )
                return
            except TransientImportError:
                if attempt >= max_attempts:
                    raise
                delay_seconds = min(
                    backoff_seconds * (2 ** (attempt - 1)),
                    backoff_max_seconds,
                )
                logger.warning(
                    "Provider feed download failed; retrying",
                    extra={
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "retry_delay_seconds": delay_seconds,
                    },
                    exc_info=True,
                )
                time.sleep(delay_seconds)

        raise RuntimeError("Provider feed download retry loop exited unexpectedly")

    @staticmethod
    def _download_zip(
        url: str,
        destination: Path,
        timeout_seconds: float,
        max_download_bytes: int,
    ) -> None:
        try:
            request = Request(
                url,
                headers={"User-Agent": "job-board-backend/0.1"},
            )
            opener = build_opener(
                ProxyHandler({}),
                _ResolvedHTTPHandler(),
                _ResolvedHTTPSHandler(),
                _ValidatedRedirectHandler(),
            )
            with (
                opener.open(request, timeout=timeout_seconds) as response,
                destination.open("wb") as output,
            ):
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError:
                        declared_size = None
                    if declared_size is not None and declared_size > max_download_bytes:
                        raise InvalidFeedArchiveError(
                            "Provider feed download exceeds configured maximum size"
                        )

                downloaded_bytes = 0
                while chunk := response.read(_DOWNLOAD_CHUNK_SIZE):
                    downloaded_bytes += len(chunk)
                    if downloaded_bytes > max_download_bytes:
                        raise InvalidFeedArchiveError(
                            "Provider feed download exceeds configured maximum size"
                        )
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
    def _extract_xml(
        zip_path: Path,
        destination: Path,
        max_extracted_bytes: int,
    ) -> None:
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
                if xml_members[0].file_size > max_extracted_bytes:
                    raise InvalidFeedArchiveError(
                        "Downloaded XML exceeds configured maximum extracted size"
                    )

                with (
                    archive.open(xml_members[0]) as source,
                    destination.open("wb") as output,
                ):
                    extracted_bytes = 0
                    while chunk := source.read(_DOWNLOAD_CHUNK_SIZE):
                        extracted_bytes += len(chunk)
                        if extracted_bytes > max_extracted_bytes:
                            raise InvalidFeedArchiveError(
                                "Downloaded XML exceeds configured maximum extracted size"
                            )
                        output.write(chunk)
        except InvalidFeedArchiveError:
            raise
        except (zipfile.BadZipFile, RuntimeError, OSError) as error:
            raise InvalidFeedArchiveError(
                f"Downloaded feed is not a readable ZIP archive: {error}"
            ) from error
