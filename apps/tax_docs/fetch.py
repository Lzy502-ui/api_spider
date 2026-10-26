"""带本地缓存、限速和重试的 HTTP 抓取。"""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

RETRY_STATUS = {403, 408, 412, 425, 429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0
    content_type: str = ""
    body: bytes = b""
    error: str = ""
    cached: bool = False
    path: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.body)

    @property
    def is_blocked(self) -> bool:
        return self.status in (403, 412, 429)


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def decode_body(body: bytes, content_type: str = "") -> str:
    charset = ""
    match = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if match:
        charset = match.group(1).lower()
    candidates = [charset] if charset else []
    candidates += ["utf-8", "gb18030", "gbk"]
    for encoding in candidates:
        if not encoding:
            continue
        try:
            return body.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="ignore")


def _filename_from_disposition(value: str) -> str:
    if not value:
        return ""
    match = re.search(r"filename\*?=(?:UTF-8''|\"?)([^;\"]+)", value, re.I)
    if not match:
        return ""
    return unquote(match.group(1).strip())


def safe_filename(name: str, fallback: str = "attachment") -> str:
    name = unquote(name or "").strip().replace("\\", "/")
    name = name.rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:180] or fallback


class Fetcher:
    def __init__(
        self,
        cache_dir: str | Path = "data/tax_docs/cache",
        *,
        delay: float = 2.0,
        timeout: float = 30.0,
        retries: int = 2,
        insecure: bool = False,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self.retries = max(0, retries)
        self.user_agent = user_agent
        self._last_request: dict[str, float] = {}
        self._ssl_context = ssl.create_default_context()
        if insecure:
            self._ssl_context.check_hostname = False
            self._ssl_context.verify_mode = ssl.CERT_NONE

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        key = _cache_key(url)
        return self.cache_dir / f"{key}.bin", self.cache_dir / f"{key}.json"

    def _load_cache(self, url: str, refresh: bool) -> FetchResult | None:
        if refresh:
            return None
        body_path, meta_path = self._cache_paths(url)
        if not body_path.exists() or not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
            body = body_path.read_bytes()
        except (OSError, json.JSONDecodeError):
            return None
        if meta.get("status") != 200:
            return None
        return FetchResult(
            url=url,
            final_url=meta.get("final_url", url),
            status=200,
            content_type=meta.get("content_type", ""),
            body=body,
            cached=True,
            path=str(body_path),
        )

    def _save_cache(self, result: FetchResult) -> None:
        body_path, meta_path = self._cache_paths(result.url)
        body_path.write_bytes(result.body)
        meta_path.write_text(
            json.dumps(
                {
                    "url": result.url,
                    "final_url": result.final_url,
                    "status": result.status,
                    "content_type": result.content_type,
                    "error": result.error,
                },
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )
        result.path = str(body_path)

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_request.get(host, 0.0)
        wait = self.delay - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        self._last_request[host] = time.monotonic()

    def _request_once(self, url: str, referer: str = "") -> FetchResult:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/pdf,application/msword,"
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        }
        if referer:
            headers["Referer"] = referer
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout, context=self._ssl_context) as response:
                body = response.read()
                status = getattr(response, "status", 200) or 200
                return FetchResult(
                    url=url,
                    final_url=response.geturl() or url,
                    status=status,
                    content_type=response.headers.get("Content-Type", ""),
                    body=body,
                )
        except HTTPError as exc:
            try:
                body = exc.read()
            except Exception:
                body = b""
            return FetchResult(
                url=url,
                final_url=getattr(exc, "url", url) or url,
                status=exc.code,
                content_type=exc.headers.get("Content-Type", "") if exc.headers else "",
                body=body,
                error=f"HTTP {exc.code}",
            )
        except (URLError, TimeoutError, OSError) as exc:
            return FetchResult(url=url, status=0, error=str(exc))

    def fetch(self, url: str, *, referer: str = "", refresh: bool = False) -> FetchResult:
        cached = self._load_cache(url, refresh)
        if cached is not None:
            return cached

        last = FetchResult(url=url)
        attempts = [url]
        if url.startswith("http://"):
            attempts.append("https://" + url[len("http://") :])
        elif url.startswith("https://"):
            attempts.append("http://" + url[len("https://") :])

        for attempt in range(self.retries + 1):
            for candidate in attempts:
                self._throttle(candidate)
                result = self._request_once(candidate, referer=referer)
                if result.ok:
                    self._save_cache(result)
                    return result
                last = result
                if result.status in (404, 410):
                    return result
            if attempt < self.retries:
                time.sleep(min(2.0 * (attempt + 1), 6.0))
        return last

    def download(self, url: str, target_dir: str | Path, *, referer: str = "") -> tuple[FetchResult, Path | None]:
        result = self.fetch(url, referer=referer)
        if not result.ok:
            return result, None
        directory = Path(target_dir)
        directory.mkdir(parents=True, exist_ok=True)
        name = ""
        # content-disposition 在 FetchResult 中未保留，按 URL/内容类型推断。
        parsed = urlparse(result.final_url or url)
        name = safe_filename(Path(parsed.path).name)
        if "." not in name:
            extension = {
                "application/pdf": ".pdf",
                "application/msword": ".doc",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            }.get((result.content_type or "").split(";")[0].strip().lower(), "")
            name += extension
        path = directory / name
        if path.exists() and path.stat().st_size == len(result.body):
            return result, path
        counter = 1
        while path.exists() and path.stat().st_size != len(result.body):
            path = directory / f"{Path(name).stem}-{counter}{Path(name).suffix}"
            counter += 1
        path.write_bytes(result.body)
        return result, path


def filename_from_headers(headers: dict[str, str]) -> str:
    return _filename_from_disposition(headers.get("Content-Disposition", ""))
