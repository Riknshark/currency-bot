import asyncio
import base64
import html as html_lib
import json
import logging
import re
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import aiohttp

logger = logging.getLogger(__name__)

NETWORK_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=7)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


def make_connector():
    return aiohttp.TCPConnector(family=socket.AF_INET, limit=20)


def _normalize_domain(url_or_domain: str) -> str:
    if "://" in url_or_domain:
        host = urlparse(url_or_domain).hostname or url_or_domain
    else:
        host = url_or_domain.split("/")[0].split(":")[0]
    return host.lower().strip()


def _decode_bytes(raw: bytes, encoding: Optional[str] = None) -> str:
    if not raw:
        return ""
    candidates = []
    if encoding:
        candidates.append(encoding)
    candidates.extend(["utf-8", "windows-1251", "latin-1"])
    for enc in candidates:
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="ignore")


class CookieStore:
    """In-memory + SQLite cookie store keyed by domain."""

    _cookies: dict[str, dict[str, str]] = {}
    _loaded: bool = False

    @classmethod
    async def load_from_db(cls):
        from database.manager import DatabaseManager

        cls._cookies = await DatabaseManager.get_all_domain_cookies()
        cls._loaded = True

    @classmethod
    def get_for_url(cls, url: str) -> dict[str, str]:
        host = _normalize_domain(url)
        merged: dict[str, str] = {}
        parts = host.split(".")
        domains_to_check = [host]
        if len(parts) >= 2:
            domains_to_check.append(".".join(parts[-2:]))
        for dom in reversed(domains_to_check):
            if dom in cls._cookies:
                merged.update(cls._cookies[dom])
        return merged

    @classmethod
    async def update_cookies(cls, url_or_domain: str, new_cookies: dict[str, str]):
        if not new_cookies:
            return
        domain = _normalize_domain(url_or_domain)
        if not domain:
            return
        current = cls._cookies.setdefault(domain, {})
        changed = False
        for k, v in new_cookies.items():
            if k and v is not None and current.get(k) != str(v):
                current[str(k)] = str(v)
                changed = True
        if changed:
            try:
                from database.manager import DatabaseManager

                await DatabaseManager.save_domain_cookies(domain, current)
            except Exception as e:
                logger.debug(f"Failed to persist cookies for {domain}: {e}")


@dataclass
class HttpResponse:
    url: str
    status: int
    headers: dict[str, str]
    content: bytes
    text: str
    cookies: dict[str, str]


@dataclass
class CaptchaFormInfo:
    action_url: str
    method: str
    text_field: str
    hidden_fields: dict[str, str] = field(default_factory=dict)


@dataclass
class CaptchaChallenge:
    domain: str
    page_url: str
    reason: str
    image_bytes: Optional[bytes] = None
    form_info: Optional[CaptchaFormInfo] = None
    cookies: dict[str, str] = field(default_factory=dict)


@dataclass
class PendingCaptchaTask:
    task_id: str
    user_id: int
    bank_id: str
    bank_name: str
    currency: str
    threshold: float
    step: float
    challenge: CaptchaChallenge
    created_at: float = field(default_factory=time.time)


class HttpClient:
    """HTTP client using curl_cffi (Chrome TLS) with aiohttp fallback & cookie persistence."""

    @staticmethod
    def _cffi_request_sync(
        method: str,
        url: str,
        headers: dict[str, str],
        cookies: dict[str, str],
        data: Optional[dict[str, str]] = None,
        params: Optional[dict[str, str]] = None,
        timeout: int = 20,
    ) -> HttpResponse:
        from curl_cffi import requests as cffi_requests

        last_err = None
        for impersonate in ("chrome", "chrome124", "chrome120", None):
            try:
                kwargs: dict[str, Any] = {}
                if impersonate:
                    kwargs["impersonate"] = impersonate
                with cffi_requests.Session(**kwargs) as session:
                    resp = session.request(
                        method=method,
                        url=url,
                        headers=headers,
                        cookies=cookies,
                        data=data,
                        params=params,
                        timeout=timeout,
                        allow_redirects=True,
                    )
                    resp_cookies = {}
                    try:
                        resp_cookies.update(dict(session.cookies.items()))
                    except Exception:
                        pass
                    try:
                        resp_cookies.update(dict(resp.cookies.items()))
                    except Exception:
                        pass
                    raw = resp.content or b""
                    text = _decode_bytes(raw, getattr(resp, "encoding", None))
                    hdrs = {str(k).lower(): str(v) for k, v in resp.headers.items()}
                    return HttpResponse(
                        url=str(resp.url),
                        status=int(resp.status_code),
                        headers=hdrs,
                        content=raw,
                        text=text,
                        cookies=resp_cookies,
                    )
            except TypeError as e:
                last_err = e
                continue
        raise last_err or RuntimeError("curl_cffi request failed")

    @classmethod
    async def request(
        cls,
        method: str,
        url: str,
        headers: Optional[dict[str, str]] = None,
        extra_cookies: Optional[dict[str, str]] = None,
        data: Optional[dict[str, str]] = None,
        params: Optional[dict[str, str]] = None,
        timeout: int = 20,
    ) -> Optional[HttpResponse]:
        req_headers = dict(BROWSER_HEADERS)
        if headers:
            req_headers.update(headers)

        cookies = CookieStore.get_for_url(url)
        if extra_cookies:
            cookies.update(extra_cookies)

        # Primary: curl_cffi
        try:
            resp = await asyncio.to_thread(
                cls._cffi_request_sync,
                method.upper(),
                url,
                req_headers,
                cookies,
                data,
                params,
                timeout,
            )
            await CookieStore.update_cookies(resp.url, resp.cookies)
            return resp
        except Exception as e:
            logger.debug(f"curl_cffi failed for {url}: {e}")

        # Fallback: aiohttp
        try:
            async with aiohttp.ClientSession(
                headers=req_headers,
                cookies=cookies,
                timeout=aiohttp.ClientTimeout(total=timeout, connect=7),
                connector=make_connector(),
            ) as session:
                async with session.request(
                    method.upper(), url, data=data, params=params, allow_redirects=True
                ) as response:
                    raw = await response.read()
                    text = _decode_bytes(raw, response.charset)
                    resp_cookies = {k: v.value for k, v in response.cookies.items()}
                    hdrs = {k.lower(): v for k, v in response.headers.items()}
                    final_url = str(response.url)
                    await CookieStore.update_cookies(final_url, resp_cookies)
                    return HttpResponse(
                        url=final_url,
                        status=response.status,
                        headers=hdrs,
                        content=raw,
                        text=text,
                        cookies=resp_cookies,
                    )
        except Exception as e:
            logger.debug(f"aiohttp failed for {url}: {e}")
            return None


class CaptchaDetector:
    """Detects CAPTCHA / WAF challenge pages and extracts image + form data."""

    SIGNATURES = [
        ("smartcaptcha.yandex", "Yandex SmartCaptcha"),
        ("captcha-api.yandex", "Yandex SmartCaptcha"),
        ("smart-captcha", "Yandex SmartCaptcha"),
        ("showcaptcha", "Yandex Captcha"),
        ("bitrix/tools/captcha.php", "1C-Bitrix Captcha"),
        ("captcha_sid", "1C-Bitrix Captcha"),
        ("ddos-guard", "DDoS-Guard"),
        ("servicepipe", "ServicePipe WAF"),
        ("qrator", "Qrator WAF"),
        ("cf-turnstile", "Cloudflare Turnstile"),
        ("cf-chl-", "Cloudflare Challenge"),
        ("attention required! | cloudflare", "Cloudflare"),
        ("just a moment...", "Cloudflare Challenge"),
        ("g-recaptcha", "Google reCAPTCHA"),
        ("hcaptcha.com", "hCaptcha"),
        ("подтвердите, что вы не робот", "Проверка капчи"),
        ("введите код с картинки", "Капча с картинки"),
        ("вы не робот", "Проверка капчи"),
        ("проверка браузера", "Защита от ботов"),
    ]

    @classmethod
    def _is_valid_non_captcha_json(cls, text: str, status: int) -> bool:
        if status >= 400:
            return False
        stripped = text.strip()
        if not stripped or stripped[0] not in ("{", "["):
            return False
        try:
            data = json.loads(stripped)
            low = stripped.lower()
            if isinstance(data, dict) and ("captcha" in low and "rates" not in low and "Valute" not in stripped):
                return False
            return True
        except Exception:
            return False

    @classmethod
    def _extract_attr(cls, tag: str, attr: str) -> Optional[str]:
        m = re.search(
            rf"""{attr}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
            tag,
            re.IGNORECASE,
        )
        if not m:
            return None
        val = m.group(1) if m.group(1) is not None else (
            m.group(2) if m.group(2) is not None else m.group(3)
        )
        return html_lib.unescape(val)

    @classmethod
    def _extract_form(cls, html: str, base_url: str) -> tuple[Optional[CaptchaFormInfo], Optional[str]]:
        forms = re.findall(r"<form\b[^>]*>.*?</form>", html, re.DOTALL | re.IGNORECASE)
        chosen_form = None
        for form_html in forms:
            low = form_html.lower()
            if any(k in low for k in ("captcha", "securimage", "challenge", "code", "answer")):
                chosen_form = form_html
                break
        if not chosen_form and forms:
            chosen_form = forms[0]
        if not chosen_form:
            return None, None

        form_open = re.match(r"<form\b[^>]*>", chosen_form, re.IGNORECASE)
        open_tag = form_open.group(0) if form_open else ""
        action_raw = cls._extract_attr(open_tag, "action") or ""
        action_url = urljoin(base_url, action_raw) if action_raw else base_url
        method = (cls._extract_attr(open_tag, "method") or "POST").upper()

        hidden_fields: dict[str, str] = {}
        text_candidates: list[str] = []

        for inp in re.findall(r"<input\b[^>]*>", chosen_form, re.IGNORECASE):
            name = cls._extract_attr(inp, "name")
            if not name:
                continue
            inp_type = (cls._extract_attr(inp, "type") or "text").lower()
            value = cls._extract_attr(inp, "value") or ""
            if inp_type == "hidden":
                hidden_fields[name] = value
            elif inp_type in ("text", "search", "tel", "number"):
                text_candidates.append(name)

        text_field = None
        for name in text_candidates:
            if any(k in name.lower() for k in ("captcha", "code", "word", "answer", "rep", "key")):
                text_field = name
                break
        if not text_field and text_candidates:
            text_field = text_candidates[0]

        img_src = cls._find_captcha_img_src(chosen_form, base_url)
        if not text_field:
            return None, img_src

        return (
            CaptchaFormInfo(
                action_url=action_url,
                method=method,
                text_field=text_field,
                hidden_fields=hidden_fields,
            ),
            img_src,
        )

    @classmethod
    def _find_captcha_img_src(cls, html: str, base_url: str) -> Optional[str]:
        imgs = re.findall(r"<img\b[^>]*>", html, re.IGNORECASE)
        for img in imgs:
            src = cls._extract_attr(img, "src")
            if not src:
                continue
            low = (img + " " + src).lower()
            if src.startswith("data:image/"):
                return src
            if any(k in low for k in ("captcha", "securimage", "showcaptcha", "challenge", "antispam")):
                return urljoin(base_url, src)
        return None

    @classmethod
    async def detect(cls, resp: HttpResponse) -> Optional[CaptchaChallenge]:
        if cls._is_valid_non_captcha_json(resp.text, resp.status):
            return None

        low = resp.text.lower()
        reason = None
        for sig, label in cls.SIGNATURES:
            if sig in low:
                reason = label
                break

        if not reason and resp.status in (403, 429, 503, 202):
            if "<html" in low or "captcha" in low or "robot" in low or "access denied" in low:
                reason = f"HTTP {resp.status} Anti-bot"

        if not reason:
            return None

        form_info, img_src = cls._extract_form(resp.text, resp.url)
        if not img_src:
            img_src = cls._find_captcha_img_src(resp.text, resp.url)

        image_bytes = None
        if img_src:
            if img_src.startswith("data:image/"):
                try:
                    _, b64 = img_src.split(",", 1)
                    image_bytes = base64.b64decode(b64)
                except Exception:
                    image_bytes = None
            else:
                img_resp = await HttpClient.request(
                    "GET",
                    img_src,
                    headers={"Referer": resp.url},
                    extra_cookies=resp.cookies,
                )
                if img_resp and img_resp.status == 200 and len(img_resp.content) > 100:
                    ctype = img_resp.headers.get("content-type", "")
                    if ctype.startswith("image/") or img_resp.content[:4] in (
                        b"\x89PNG",
                        b"\xff\xd8\xff\xe0",
                        b"\xff\xd8\xff\xe1",
                        b"GIF8",
                        b"RIFF",
                    ):
                        image_bytes = img_resp.content

        return CaptchaChallenge(
            domain=_normalize_domain(resp.url),
            page_url=resp.url,
            reason=reason,
            image_bytes=image_bytes,
            form_info=form_info,
            cookies=dict(resp.cookies),
        )


class CaptchaSolver:
    """Applies a human's answer or browser cookies to resolve a CaptchaChallenge."""

    @staticmethod
    def _parse_cookie_string(raw: str) -> dict[str, str]:
        text = raw.strip()
        if text.lower().startswith("cookie:"):
            text = text[7:].strip()
        cookies: dict[str, str] = {}
        for part in text.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip()
                v = v.strip()
                if k and " " not in k:
                    cookies[k] = v
        return cookies

    @classmethod
    async def submit_solution(
        cls, challenge: CaptchaChallenge, user_input: str
    ) -> tuple[bool, Optional[CaptchaChallenge], Optional[str]]:
        answer = (user_input or "").strip()
        if not answer:
            return False, None, "Пустой ответ. Введите текст капчи или cookies."

        # Check if user supplied a Cookie string
        looks_like_cookies = ("=" in answer) and (
            answer.lower().startswith("cookie:")
            or ";" in answer
            or any(
                key in answer
                for key in (
                    "cf_clearance=",
                    "qrator_",
                    "ddg",
                    "PHPSESSID=",
                    "BITRIX_",
                    "smartcaptcha",
                    "sp_",
                )
            )
            or challenge.form_info is None
        )

        if looks_like_cookies:
            parsed = cls._parse_cookie_string(answer)
            if not parsed:
                return False, None, "Не удалось распознать cookies. Формат: name=value; name2=value2"
            await CookieStore.update_cookies(challenge.domain, parsed)
            resp = await HttpClient.request("GET", challenge.page_url)
            if not resp:
                return False, None, "Не удалось повторно открыть страницу."
            new_cap = await CaptchaDetector.detect(resp)
            if new_cap:
                return False, new_cap, "С этими cookies защита всё ещё активна."
            return True, None, None

        # Submit form with captcha text
        if challenge.form_info:
            fi = challenge.form_info
            payload = dict(fi.hidden_fields)
            payload[fi.text_field] = answer
            headers = {"Referer": challenge.page_url, "Origin": f"https://{challenge.domain}"}
            if fi.method == "GET":
                resp = await HttpClient.request(
                    "GET",
                    fi.action_url,
                    headers=headers,
                    extra_cookies=challenge.cookies,
                    params=payload,
                )
            else:
                resp = await HttpClient.request(
                    "POST",
                    fi.action_url,
                    headers=headers,
                    extra_cookies=challenge.cookies,
                    data=payload,
                )
            if not resp:
                return False, None, "Сетевая ошибка при отправке капчи."

            new_cap = await CaptchaDetector.detect(resp)
            if new_cap:
                return False, new_cap, "❌ Неверный код капчи, попробуйте ещё раз:"

            if resp.status < 400:
                return True, None, None
            return False, None, f"Сервер вернул статус {resp.status}. Попробуйте обновить капчу."

        # No form (interactive JS challenge) — try refreshing
        resp = await HttpClient.request("GET", challenge.page_url)
        if resp:
            new_cap = await CaptchaDetector.detect(resp)
            if not new_cap and resp.status < 400:
                return True, None, None
            return (
                False,
                new_cap,
                "Это интерактивная защита без текстового поля. Откройте ссылку в браузере, "
                "пройдите проверку и вставьте сюда строку Cookie (или нажмите «Повторить проверку»).",
            )
        return False, None, "Не удалось проверить страницу."


class CaptchaManager:
    """Tracks pending human-solved CAPTCHA tasks per Telegram user."""

    TTL_SECONDS = 900  # 15 minutes
    PROMPT_COOLDOWN = 600  # avoid spamming same domain captcha every minute

    _pending: dict[int, PendingCaptchaTask] = {}
    _last_prompted: dict[tuple[int, str], float] = {}

    @classmethod
    def _cleanup_expired(cls):
        now = time.time()
        expired = [
            uid for uid, task in cls._pending.items()
            if now - task.created_at > cls.TTL_SECONDS
        ]
        for uid in expired:
            cls._pending.pop(uid, None)

    @classmethod
    def has_pending(cls, user_id: int) -> bool:
        cls._cleanup_expired()
        return user_id in cls._pending

    @classmethod
    def get_pending(cls, user_id: int) -> Optional[PendingCaptchaTask]:
        cls._cleanup_expired()
        return cls._pending.get(user_id)

    @classmethod
    def get_by_id(cls, task_id: str) -> Optional[PendingCaptchaTask]:
        cls._cleanup_expired()
        for task in cls._pending.values():
            if task.task_id == task_id:
                return task
        return None

    @classmethod
    def should_prompt(cls, user_id: int, domain: str, force: bool = False) -> bool:
        if force:
            return True
        cls._cleanup_expired()
        if user_id in cls._pending:
            return False
        last = cls._last_prompted.get((user_id, domain), 0.0)
        return (time.time() - last) >= cls.PROMPT_COOLDOWN

    @classmethod
    def create_task(
        cls,
        user_id: int,
        bank_id: str,
        bank_name: str,
        currency: str,
        threshold: float,
        step: float,
        challenge: CaptchaChallenge,
    ) -> PendingCaptchaTask:
        task = PendingCaptchaTask(
            task_id=uuid.uuid4().hex[:10],
            user_id=user_id,
            bank_id=bank_id,
            bank_name=bank_name,
            currency=currency,
            threshold=threshold,
            step=step,
            challenge=challenge,
        )
        cls._pending[user_id] = task
        cls._last_prompted[(user_id, challenge.domain)] = time.time()
        return task

    @classmethod
    def update_challenge(cls, user_id: int, challenge: CaptchaChallenge) -> Optional[PendingCaptchaTask]:
        task = cls._pending.get(user_id)
        if task:
            task.challenge = challenge
            task.created_at = time.time()
        return task

    @classmethod
    def clear(cls, user_id: int):
        cls._pending.pop(user_id, None)