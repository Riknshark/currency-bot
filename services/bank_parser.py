"""
Bank rate parsers for currency_bot.
fetch_rate(currency) -> Optional[float].
Semantics: returns the rate at which the BANK SELLS currency to you
(the price you pay when buying), unless noted otherwise.
"""
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from services.captcha_bridge import CaptchaChallenge, CaptchaDetector, HttpClient, HttpResponse

ISO_NUMERIC = {
    "USD": "840",
    "EUR": "978",
    "CNY": "156",
    "GBP": "826",
    "CHF": "756",
    "JPY": "392",
    "KZT": "398",
    "TRY": "949",
    "AED": "784",
    "BYN": "933",
}


@dataclass
class FetchResult:
    rate: Optional[float] = None
    source: str = ""
    captcha: Optional[CaptchaChallenge] = None
    error: Optional[str] = None


def _strip_tags(html: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def _html_to_table_lines(html: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"</t[dh]>", " | ", text, flags=re.IGNORECASE)
    text = re.sub(r"<tr[^>]*>", "\n| ", text, flags=re.IGNORECASE)
    text = re.sub(r"</tr>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text)


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        val = float(value)
        return val if val > 0 else None
    try:
        cleaned = str(value).replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
        val = float(cleaned)
        return val if val > 0 else None
    except (TypeError, ValueError):
        return None


def _clean_label(label: str, currency: str) -> str:
    tokens = []
    for token in reversed(label.split()):
        if token == currency.upper():
            continue
        if token[:1].isupper() or token.isupper():
            tokens.append(token)
        else:
            break
    return " ".join(reversed(tokens))


async def _fetch_and_check(
    url: str,
    headers: Optional[dict[str, str]] = None,
    timeout: int = 20,
) -> tuple[Optional[HttpResponse], Optional[CaptchaChallenge], Optional[str]]:
    resp = await HttpClient.request("GET", url, headers=headers, timeout=timeout)
    if not resp:
        return None, None, f"Сетевая ошибка: {url}"
    captcha = await CaptchaDetector.detect(resp)
    if captcha:
        return resp, captcha, captcha.reason
    if resp.status != 200:
        return resp, None, f"HTTP {resp.status}: {url}"
    return resp, None, None


class BaseBankParser(ABC):
    @abstractmethod
    async def fetch_rate_detailed(self, currency: str) -> FetchResult:
        pass

    async def fetch_rate(self, currency: str) -> Optional[float]:
        res = await self.fetch_rate_detailed(currency)
        return res.rate


class CompositeBankParser(BaseBankParser):
    """Tries multiple sources in priority order; propagates CAPTCHA if all fail."""

    def __init__(self, parsers: list[tuple[str, BaseBankParser]]):
        self.parsers = parsers

    async def fetch_rate_detailed(self, currency: str) -> FetchResult:
        best_captcha: Optional[CaptchaChallenge] = None
        cap_source = ""
        last_error = "Источники недоступны"

        for name, parser in self.parsers:
            res = await parser.fetch_rate_detailed(currency)
            if res.rate is not None and res.rate > 0:
                return FetchResult(rate=res.rate, source=res.source or name)
            if res.captcha:
                if best_captcha is None or (
                    res.captcha.image_bytes is not None and best_captcha.image_bytes is None
                ):
                    best_captcha = res.captcha
                    cap_source = res.source or name
            if res.error:
                last_error = res.error

        if best_captcha is not None:
            return FetchResult(
                rate=None,
                source=cap_source,
                captcha=best_captcha,
                error=best_captcha.reason,
            )
        return FetchResult(rate=None, error=last_error)


class UniversalJsonParser(BaseBankParser):
    def __init__(self, api_url: str, json_path: str, api_key: Optional[str] = None):
        self.api_url = api_url
        self.json_path_template = json_path or ""
        self.api_key = api_key

    async def fetch_rate_detailed(self, currency: str) -> FetchResult:
        if not self.api_url:
            return FetchResult(error="API URL не настроен")
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = self.api_url.replace("{currency}", currency.upper())
        path_keys = (
            self.json_path_template.replace("{currency}", currency.upper()).split(".")
            if self.json_path_template
            else []
        )
        resp, captcha, err = await _fetch_and_check(url, headers=headers)
        if captcha:
            return FetchResult(source="Custom API", captcha=captcha, error=err)
        if not resp or err:
            return FetchResult(error=err)
        try:
            data = json.loads(resp.text)
            val = self._extract_value(data, path_keys)
            if val is not None:
                return FetchResult(rate=val, source="Custom API")
            return FetchResult(error="Курс не найден по JSON-пути")
        except Exception as e:
            return FetchResult(error=f"Ошибка JSON: {e}")

    def _extract_value(self, data: Any, path_keys: list[str]) -> Optional[float]:
        current = data
        for key in path_keys:
            if not key:
                continue
            if isinstance(current, dict) and key in current:
                current = current[key]
            elif isinstance(current, list) and key.isdigit():
                idx = int(key)
                if 0 <= idx < len(current):
                    current = current[idx]
                else:
                    return None
            else:
                return None
        return _to_float(current)


class TBankDirectParser(BaseBankParser):
    """Official T-Bank public JSON API (api.tbank.ru / api.tinkoff.ru)."""

    ENDPOINTS = (
        "https://api.tbank.ru/v1/currency_rates?from={cur}&to=RUB",
        "https://api.tinkoff.ru/v1/currency_rates?from={cur}&to=RUB",
    )
    CATEGORIES = (
        "DebitCardsTransfers",
        "C2CTransfers",
        "DepositPayments",
        "SavingAccountTransfers",
    )

    def __init__(self, rate_type: str = "sell"):
        self.rate_type = rate_type

    async def fetch_rate_detailed(self, currency: str) -> FetchResult:
        cur = currency.upper()
        last_err = None
        last_cap = None
        for template in self.ENDPOINTS:
            url = template.format(cur=cur)
            resp, captcha, err = await _fetch_and_check(url)
            if captcha:
                last_cap = captcha
                continue
            if not resp or err:
                last_err = err
                continue
            try:
                data = json.loads(resp.text)
                rates = (data.get("payload") or {}).get("rates") or []
                matching = [
                    r for r in rates
                    if isinstance(r, dict)
                    and (r.get("fromCurrency") or {}).get("name", "").upper() == cur
                    and (r.get("toCurrency") or {}).get("name", "").upper() == "RUB"
                ]
                chosen = None
                for cat in self.CATEGORIES:
                    for r in matching:
                        if r.get("category") == cat:
                            chosen = r
                            break
                    if chosen:
                        break
                if not chosen and matching:
                    chosen = matching[0]
                if chosen:
                    val = _to_float(chosen.get("sell" if self.rate_type == "sell" else "buy"))
                    if val is not None:
                        return FetchResult(rate=val, source="T-Bank API")
            except Exception as e:
                last_err = str(e)
        return FetchResult(source="T-Bank API", captcha=last_cap, error=last_err)


class SberDirectParser(BaseBankParser):
    """Official Sberbank web JSON endpoints."""

    def __init__(self, rate_type: str = "sell"):
        self.rate_type = rate_type

    @staticmethod
    def _find_beznal_rate(obj: Any, rate_key: str) -> Optional[float]:
        if isinstance(obj, dict):
            if rate_key in obj:
                lot = _to_float(obj.get("lotSize")) or 1.0
                val = _to_float(obj.get(rate_key))
                if val is not None:
                    return val / lot
            for v in obj.values():
                res = SberDirectParser._find_beznal_rate(v, rate_key)
                if res is not None:
                    return res
        elif isinstance(obj, list):
            for item in obj:
                res = SberDirectParser._find_beznal_rate(item, rate_key)
                if res is not None:
                    return res
        return None

    async def fetch_rate_detailed(self, currency: str) -> FetchResult:
        cur = currency.upper()
        urls = [
            f"https://www.sberbank.ru/proxy/services/rates/public/actual?rateType=ERNP-2&isoCodes[]={cur}&regionId=038",
            f"https://www.sberbank.ru/proxy/services/rates/public/actual?rateType=ERNP-4&isoCodes[]={cur}&regionId=038",
            f"https://www.sberbank.ru/proxy/services/rates/public/actual?rateType=ERNP-2&isoCodes[]={cur}&regionId=77",
        ]
        num_code = ISO_NUMERIC.get(cur)
        if num_code:
            urls.append(
                f"https://www.sberbank.ru/portalserver/proxy/?pipe=shortCachePipe"
                f"&url=http://localhost/rates-web/rateService/rate/current"
                f"%3FregionId%3D77%26currencyCode%3D{num_code}%26rateCategory%3Dbeznal"
            )

        best_cap = None
        last_err = None
        headers = {"Referer": "https://www.sberbank.ru/ru/quotes/currencies"}

        for url in urls:
            resp, captcha, err = await _fetch_and_check(url, headers=headers)
            if captcha:
                best_cap = best_cap or captcha
                continue
            if not resp or err:
                last_err = err
                continue
            try:
                data = json.loads(resp.text)
                if cur in data and isinstance(data[cur], dict):
                    lot = _to_float(data[cur].get("lotSize")) or 1.0
                    rate_list = data[cur].get("rateList") or []
                    if rate_list and isinstance(rate_list[0], dict):
                        key = "rateSell" if self.rate_type == "sell" else "rateBuy"
                        val = _to_float(rate_list[0].get(key))
                        if val is not None:
                            return FetchResult(rate=val / lot, source="Sberbank API")
                key2 = "sellValue" if self.rate_type == "sell" else "buyValue"
                val2 = self._find_beznal_rate(data, key2)
                if val2 is not None:
                    return FetchResult(rate=val2, source="Sberbank Legacy API")
            except Exception as e:
                last_err = str(e)

        return FetchResult(source="Sberbank", captcha=best_cap, error=last_err)


class AlfaDirectParser(BaseBankParser):
    """Alfa-Bank currency page / embedded JSON parser."""

    URL = "https://alfabank.ru/currency/"

    def __init__(self, rate_type: str = "sell"):
        self.rate_type = rate_type

    async def fetch_rate_detailed(self, currency: str) -> FetchResult:
        cur = currency.upper()
        resp, captcha, err = await _fetch_and_check(self.URL)
        if captcha:
            return FetchResult(source="Alfa-Bank", captcha=captcha, error=err)
        if not resp or err:
            return FetchResult(error=err)

        html = resp.text
        # Try extracting JSON-embedded rate for currency
        m = re.search(
            rf'"(?:currency|isoCode|code)"\s*:\s*"{cur}".*?'
            rf'"(?:sell|sellRate|sale)"\s*:\s*"?(\d+[.,]?\d*)"?',
            html,
            re.DOTALL | re.IGNORECASE,
        )
        if m and self.rate_type == "sell":
            val = _to_float(m.group(1))
            if val:
                return FetchResult(rate=val, source="Alfa-Bank")

        # Try table/text parsing
        lines = _html_to_table_lines(html)
        for line in lines.splitlines():
            if cur in line.upper():
                nums = [_to_float(x) for x in re.findall(r"\b\d{1,4}[.,]\d{2,4}\b", line)]
                nums = [n for n in nums if n is not None]
                if len(nums) >= 2:
                    buy_v, sell_v = min(nums[0], nums[1]), max(nums[0], nums[1])
                    return FetchResult(
                        rate=sell_v if self.rate_type == "sell" else buy_v,
                        source="Alfa-Bank",
                    )

        return FetchResult(error="Не удалось извлечь курс со страницы Alfa-Bank")


class AtbDirectParser(BaseBankParser):
    """Asian-Pacific Bank (atb.su & eng.atb.su) direct parser."""

    RU_URL = "https://www.atb.su/services/currency-exchange/"
    EN_URL = "https://www.eng.atb.su/individual-clients/"

    CURRENCY_RU = {
        "USD": "доллар",
        "EUR": "евро",
        "CNY": "юань",
        "GBP": "фунт",
        "CHF": "франк",
        "JPY": "иен",
    }

    def __init__(self, rate_type: str = "sell"):
        self.rate_type = rate_type

    async def fetch_rate_detailed(self, currency: str) -> FetchResult:
        cur = currency.upper()
        best_cap = None
        last_err = None

        # 1. Try atb.su/services/currency-exchange/
        resp, captcha, err = await _fetch_and_check(self.RU_URL)
        if captcha:
            best_cap = captcha
        elif resp and not err:
            lines = _html_to_table_lines(resp.text)
            ru_name = self.CURRENCY_RU.get(cur, cur.lower())
            for line in lines.splitlines():
                low = line.lower()
                if cur.lower() in low or ru_name in low:
                    nums = [_to_float(x) for x in re.findall(r"\b\d{1,4}[.,]\d{2,4}\b", line)]
                    nums = [n for n in nums if n is not None]
                    if len(nums) >= 2:
                        buy_v, sell_v = min(nums[0], nums[1]), max(nums[0], nums[1])
                        return FetchResult(
                            rate=sell_v if self.rate_type == "sell" else buy_v,
                            source="ATB (atb.su)",
                        )
            last_err = "Курс не найден на atb.su"
        else:
            last_err = err

        # 2. Try eng.atb.su/individual-clients/
        resp_en, cap_en, err_en = await _fetch_and_check(self.EN_URL)
        if cap_en and not best_cap:
            best_cap = cap_en
        elif resp_en and not err_en:
            plain = _strip_tags(resp_en.text)
            m = re.search(
                r"CURRENCY\s+(.*?)\s+WE\s+BUY\s+(.*?)\s+WE\s+SELL\s+(.*?)(?:CURRENCY|High|$)",
                plain,
                re.IGNORECASE,
            )
            if m:
                codes = re.findall(r"\b[A-Z]{3}\b", m.group(1))
                buys = [_to_float(x) for x in re.findall(r"\d+[.,]\d+", m.group(2))]
                sells = [_to_float(x) for x in re.findall(r"\d+[.,]\d+", m.group(3))]
                if cur in codes:
                    idx = codes.index(cur)
                    target = sells if self.rate_type == "sell" else buys
                    if idx < len(target) and target[idx] is not None:
                        return FetchResult(rate=target[idx], source="ATB (eng.atb.su)")
            last_err = "Курс не найден на eng.atb.su"
        elif err_en:
            last_err = err_en

        return FetchResult(source="ATB", captcha=best_cap, error=last_err)


class IngobankPageParser(BaseBankParser):
    """Ingobank internet-bank rates from /filesonload/dsource.js."""

    PAGE_URL = "https://ingobank.ru/currency/"
    CUR_INDEX = {"USD": (0, 1), "EUR": (2, 3), "CHF": (4, 5), "GBP": (6, 7), "CNY": (8, 9)}

    def __init__(self, rate_type: str = "sell"):
        self.rate_type = rate_type

    async def fetch_rate_detailed(self, currency: str) -> FetchResult:
        idx = self.CUR_INDEX.get(currency.upper())
        if idx is None:
            return FetchResult(error=f"Валюта {currency} не поддерживается на ingobank.ru")
        buy_idx, sell_idx = idx

        resp, captcha, err = await _fetch_and_check(self.PAGE_URL)
        if captcha:
            return FetchResult(source="Ingobank", captcha=captcha, error=err)
        if not resp or err:
            return FetchResult(error=err)

        m = re.search(r"(/filesonload/dsource\.js\?\d+)", resp.text)
        if not m:
            return FetchResult(error="Не найден dsource.js на ingobank.ru")

        js_url = "https://ingobank.ru" + m.group(1)
        js_resp, js_cap, js_err = await _fetch_and_check(
            js_url, headers={"Referer": self.PAGE_URL}
        )
        if js_cap:
            return FetchResult(source="Ingobank", captcha=js_cap, error=js_err)
        if not js_resp or js_err:
            return FetchResult(error=js_err)

        js = js_resp.text
        if "arr_Offices" not in js:
            try:
                js = js_resp.content.decode("windows-1251", errors="ignore")
            except Exception:
                pass

        block = re.search(r"var\s+arr_Offices\s*=\s*new\s+Array\((.*?)\)\s*;", js, re.DOTALL)
        if not block:
            block = re.search(r"var\s+arr_Offices\s*=\s*new\s+Array\((.*?)\)", js, re.DOTALL)
        if not block:
            return FetchResult(error="Не найден массив arr_Offices в dsource.js")

        elements = re.findall(r"'([^']*)'|\"([^\"]*)\"", block.group(1))
        offices = []
        current = None
        for name_match, num_match in elements:
            if name_match:
                if current is not None and len(current["numbers"]) == 10:
                    offices.append(current)
                current = {"name": name_match, "numbers": []}
            elif num_match and current is not None:
                value = _to_float(num_match)
                if value is not None:
                    current["numbers"].append(value)
        if current is not None and len(current["numbers"]) == 10:
            offices.append(current)
        if not offices:
            return FetchResult(error="Список отделений Ingobank пуст")

        online = [
            o for o in offices
            if "интернет" in o["name"].lower() or "онлайн" in o["name"].lower()
        ]
        if not online:
            online = [
                min(
                    offices,
                    key=lambda o: o["numbers"][sell_idx] if o["numbers"][sell_idx] > 0 else 10**9,
                )
            ]

        best = None
        for office in online:
            rate = office["numbers"][sell_idx if self.rate_type == "sell" else buy_idx]
            if rate > 0:
                if best is None:
                    best = rate
                elif self.rate_type == "sell" and rate < best:
                    best = rate
                elif self.rate_type == "buy" and rate > best:
                    best = rate

        if best is not None:
            return FetchResult(rate=best, source="Ingobank (dsource.js)")
        return FetchResult(error="Нулевой курс на ingobank.ru")


class IngobankParser(BaseBankParser):
    """Legacy: reads rates from local cache."""

    CACHE_PATH = os.getenv("INGO_CACHE_PATH", "/root/currency_bot/cache/ingo_rates.json")
    MAX_CACHE_AGE = 26 * 3600

    def __init__(self, api_url: str = "", api_key: Optional[str] = None, rate_type: str = "sell"):
        self.api_url = api_url
        self.api_key = api_key
        self.rate_type = rate_type

    async def fetch_rate_detailed(self, currency: str) -> FetchResult:
        try:
            if not os.path.exists(self.CACHE_PATH):
                return FetchResult(error="Локальный кэш Ingobank отсутствует")
            with open(self.CACHE_PATH, encoding="utf-8") as f:
                payload = json.load(f)
            if time.time() - payload.get("fetched_at", 0) > self.MAX_CACHE_AGE:
                return FetchResult(error="Локальный кэш Ingobank устарел")
            items = payload.get("data")
            if isinstance(items, dict):
                items = items.get("data", [])
            if not isinstance(items, list):
                return FetchResult(error="Некорректный формат кэша Ingobank")
            for item in items:
                if (
                    isinstance(item, dict)
                    and item.get("currIso") == currency.upper()
                    and (item.get("rateType") or {}).get("code") == self.rate_type
                ):
                    val = _to_float(item.get("currRate"))
                    if val is not None:
                        return FetchResult(rate=val, source="Ingobank Cache")
            return FetchResult(error="Валюта не найдена в кэше Ingobank")
        except Exception as e:
            return FetchResult(error=str(e))


class SravniBankParser(BaseBankParser):
    """Retail rate of one bank from sravni.ru aggregator."""

    URL_TEMPLATE = "https://www.sravni.ru/bank/{slug}/valjuty/{currency}/"

    def __init__(self, bank_slug: str, rate_type: str = "customer_buy"):
        self.bank_slug = (bank_slug or "").strip("/")
        self.rate_type = rate_type

    async def fetch_rate_detailed(self, currency: str) -> FetchResult:
        if not self.bank_slug:
            return FetchResult(error="Не указан slug банка для sravni.ru")
        url = self.URL_TEMPLATE.format(slug=self.bank_slug, currency=currency.lower())
        resp, captcha, err = await _fetch_and_check(url)
        if captcha:
            return FetchResult(source=f"sravni.ru ({self.bank_slug})", captcha=captcha, error=err)
        if not resp or err:
            return FetchResult(error=err)

        text = _strip_tags(resp.text)
        m = re.search(r"Курс по\s+([^0-9]{2,40}?)\s*(\d+[.,]\d+)\s*₽\s*=\s*1", text)
        if m and "ЦБ" not in m.group(1):
            if self.rate_type == "customer_sell":
                pair = re.search(
                    r"(\d+[.,]\d+)\s*₽\s*Купить\s*(\d+[.,]\d+)\s*₽\s*Продать", text
                )
                if pair:
                    val = _to_float(pair.group(2))
                    if val:
                        return FetchResult(rate=val, source="sravni.ru")
            val = _to_float(m.group(2))
            if val:
                return FetchResult(rate=val, source="sravni.ru")

        # Alternative order on sravni.ru: "83,84 ₽ Купить 80,19 ₽ Продать"
        pair2 = re.search(r"(\d+[.,]\d+)\s*₽\s*Купить\s*(\d+[.,]\d+)\s*₽\s*Продать", text)
        if pair2:
            val = _to_float(pair2.group(2 if self.rate_type == "customer_sell" else 1))
            if val:
                return FetchResult(rate=val, source="sravni.ru")

        # Alternative order: "Купить 83,84 ₽ Продать 80,19 ₽"
        pair3 = re.search(r"Купить\s*(\d+[.,]\d+)\s*₽.*?Продать\s*(\d+[.,]\d+)\s*₽", text)
        if pair3:
            val = _to_float(pair3.group(2 if self.rate_type == "customer_sell" else 1))
            if val:
                return FetchResult(rate=val, source="sravni.ru")

        return FetchResult(error=f"Курс не найден на sravni.ru ({self.bank_slug})")


class SravniAggregatorParser(BaseBankParser):
    """Best (lowest) customer buy rate across all banks offered on a sravni.ru currency page."""

    URL_TEMPLATE = "https://www.sravni.ru/bank/{slug}/valjuty/{currency}/"
    SOURCE_SLUG = "alfa-bank"

    async def fetch_rate_detailed(self, currency: str) -> FetchResult:
        url = self.URL_TEMPLATE.format(slug=self.SOURCE_SLUG, currency=currency.lower())
        resp, captcha, err = await _fetch_and_check(url)
        if captcha:
            return FetchResult(source="sravni.ru", captcha=captcha, error=err)
        if not resp or err:
            return FetchResult(error=err)

        rates = self._parse_rates(resp.text, currency)
        if not rates:
            return FetchResult(error="Не удалось разобрать таблицу курсов sravni.ru")
        return FetchResult(rate=min(buy for buy, _ in rates.values()), source="sravni.ru")

    def _parse_rates(self, html: str, currency: str) -> dict:
        text = _strip_tags(html)
        result = {}
        m = re.search(r"Курс по\s+([^0-9]{2,40}?)\s*(\d+[.,]\d+)\s*₽\s*=\s*1", text)
        pair = re.search(r"(\d+[.,]\d+)\s*₽\s*Купить\s*(\d+[.,]\d+)\s*₽\s*Продать", text)
        if m and "ЦБ" not in m.group(1) and pair:
            label = _clean_label(m.group(1), currency) or self.SOURCE_SLUG
            result[label] = (_to_float(pair.group(1)), _to_float(pair.group(2)))
        for label, buy, sell in re.findall(
            r"([А-ЯA-Za-zЁё\- ]{2,30}?)\s*Обновлен\s*\d+\s*мин\s*назад\s*"
            r"(\d+[.,]\d+)\s*₽\s*Купить\s*(\d+[.,]\d+)\s*₽\s*Продать",
            text,
        ):
            clean = _clean_label(label, currency)
            if clean:
                result[clean] = (_to_float(buy), _to_float(sell))
        return {k: v for k, v in result.items() if v[0] and v[1]}


class MyfinBankParser(BaseBankParser):
    """Bank rates via ru.myfin.by.
    rate_type='sell' -> bank sell (you buy); 'buy' -> bank buy (you sell)."""

    URL_TEMPLATE = "https://ru.myfin.by/bank/{slug}/currency/moskva"
    CURRENCY_NAMES = {
        "USD": ("Доллар", "USD"),
        "EUR": ("Евро", "EUR"),
        "CNY": ("Юань", "CNY"),
        "GBP": ("Фунт", "GBP"),
        "CHF": ("Франк", "CHF"),
    }

    def __init__(self, bank_slug: str = "ingobank", rate_type: str = "sell"):
        self.bank_slug = (bank_slug or "").strip("/")
        self.rate_type = rate_type

    async def fetch_rate_detailed(self, currency: str) -> FetchResult:
        names = self.CURRENCY_NAMES.get(currency.upper())
        if not names:
            return FetchResult(error=f"Валюта {currency} не поддерживается на myfin.by")

        url = self.URL_TEMPLATE.format(slug=self.bank_slug)
        resp, captcha, err = await _fetch_and_check(url)
        if captcha:
            return FetchResult(source=f"myfin.by ({self.bank_slug})", captcha=captcha, error=err)
        if not resp or err:
            return FetchResult(error=err)

        table_text = _html_to_table_lines(resp.text)
        for line in table_text.splitlines():
            if any(n.lower() in line.lower() for n in names):
                nums = [_to_float(x) for x in re.findall(r"\b\d{1,4}[.,]\d{2,4}\b", line)]
                nums = [n for n in nums if n is not None]
                if len(nums) >= 2:
                    # Table columns: Валюта | Покупка (bank buys) | Продажа (bank sells) | ЦБ
                    buy_v, sell_v = nums[0], nums[1]
                    if buy_v > sell_v and len(nums) >= 2:
                        buy_v, sell_v = sell_v, buy_v
                    return FetchResult(
                        rate=sell_v if self.rate_type == "sell" else buy_v,
                        source="ru.myfin.by",
                    )
        return FetchResult(error=f"Курс не найден на ru.myfin.by ({self.bank_slug})")


class MyfinIngobankParser(MyfinBankParser):
    def __init__(self, rate_type: str = "sell"):
        super().__init__(bank_slug="ingobank", rate_type=rate_type)


class MoexParser(BaseBankParser):
    """MOEX ISS currency rates: CETS TOM -> CETS TOD -> FIXME index (works 24/7)."""

    TOM_URL = "https://iss.moex.com/iss/engines/currency/markets/selt/boards/CETS/securities/{cur}RUB_TOM.json?iss.meta=off"
    TOD_URL = "https://iss.moex.com/iss/engines/currency/markets/selt/boards/CETS/securities/{cur}RUB_TOD.json?iss.meta=off"
    FIX_URL = "https://iss.moex.com/iss/engines/currency/markets/index/securities/{cur}FIXME.json?iss.meta=off"

    @staticmethod
    def _extract_from_block(block: dict, col_names: tuple[str, ...]) -> Optional[float]:
        cols = block.get("columns") or []
        rows = block.get("data") or []
        if not cols or not rows:
            return None
        row = rows[0]
        for col in col_names:
            if col in cols:
                idx = cols.index(col)
                if idx < len(row):
                    val = _to_float(row[idx])
                    if val is not None:
                        return val
        return None

    async def fetch_rate_detailed(self, currency: str) -> FetchResult:
        cur = currency.upper()
        endpoints = [
            (
                self.TOM_URL.format(cur=cur),
                "MOEX CETS TOM",
                ("LAST", "LCURRENTPRICE", "MARKETPRICE", "OFFER", "BID"),
                ("PREVPRICE", "PREVWAPRICE"),
            ),
            (
                self.TOD_URL.format(cur=cur),
                "MOEX CETS TOD",
                ("LAST", "LCURRENTPRICE", "MARKETPRICE", "OFFER", "BID"),
                ("PREVPRICE", "PREVWAPRICE"),
            ),
            (
                self.FIX_URL.format(cur=cur),
                "MOEX FIXME",
                ("CURRENTVALUE", "LASTVALUE", "LAST"),
                ("PREVVALUE", "PREVPRICE"),
            ),
        ]

        last_err = "Нет данных MOEX"
        for url, label, md_cols, sec_cols in endpoints:
            resp, captcha, err = await _fetch_and_check(url)
            if captcha:
                return FetchResult(source=label, captcha=captcha, error=err)
            if not resp or err:
                last_err = err or last_err
                continue
            try:
                data = json.loads(resp.text)
                val = self._extract_from_block(data.get("marketdata") or {}, md_cols)
                if val is None:
                    val = self._extract_from_block(data.get("securities") or {}, sec_cols)
                if val is not None:
                    return FetchResult(rate=val, source=label)
            except Exception as e:
                last_err = str(e)

        return FetchResult(error=last_err)


class CbrfParser(BaseBankParser):
    API_URL = "https://www.cbr.ru/scripts/XML_daily.asp"

    async def fetch_rate_detailed(self, currency: str) -> FetchResult:
        cur = currency.upper()
        resp, captcha, err = await _fetch_and_check(self.API_URL)
        if captcha:
            return FetchResult(source="CBRF XML", captcha=captcha, error=err)
        if not resp or err:
            return FetchResult(error=err)
        try:
            xml_text = resp.content.decode("windows-1251", errors="replace")
            root = ET.fromstring(xml_text)
            for valute in root.findall("Valute"):
                code = valute.find("CharCode")
                if code is not None and (code.text or "").strip().upper() == cur:
                    value = _to_float(valute.find("Value").text)
                    nominal = _to_float(valute.find("Nominal").text) or 1.0
                    if value is not None:
                        return FetchResult(rate=value / nominal, source="CBRF XML")
            return FetchResult(error=f"Валюта {cur} не найдена в ЦБ РФ")
        except Exception as e:
            return FetchResult(error=f"Ошибка XML ЦБ РФ: {e}")


class CbrfJsonParser(BaseBankParser):
    API_URL = "https://www.cbr-xml-daily.ru/daily_json.js"

    async def fetch_rate_detailed(self, currency: str) -> FetchResult:
        cur = currency.upper()
        resp, captcha, err = await _fetch_and_check(self.API_URL)
        if captcha:
            return FetchResult(source="CBRF JSON", captcha=captcha, error=err)
        if not resp or err:
            return FetchResult(error=err)
        try:
            data = json.loads(resp.text)
            item = (data.get("Valute") or {}).get(cur)
            if not item:
                return FetchResult(error=f"Валюта {cur} не найдена в cbr-xml-daily.ru")
            val = _to_float(item.get("Value"))
            nominal = _to_float(item.get("Nominal")) or 1.0
            if val is not None:
                return FetchResult(rate=val / nominal, source="CBRF JSON")
            return FetchResult(error="Некорректное значение курса ЦБ РФ")
        except Exception as e:
            return FetchResult(error=f"Ошибка JSON ЦБ РФ: {e}")


class ParserFactory:
    @staticmethod
    def get_parser(
        parser_type: str,
        api_url: str = "",
        json_path: str = "",
        api_key: Optional[str] = None,
    ) -> Optional[BaseBankParser]:
        if parser_type == "universal_json":
            return UniversalJsonParser(api_url, json_path, api_key)

        if parser_type == "sber":
            return CompositeBankParser([
                ("Sberbank API", SberDirectParser()),
                ("sravni.ru", SravniBankParser("sberbank")),
                ("ru.myfin.by", MyfinBankParser("sberbank")),
            ])

        if parser_type == "alfa":
            return CompositeBankParser([
                ("Alfa-Bank", AlfaDirectParser()),
                ("sravni.ru", SravniBankParser("alfa-bank")),
                ("ru.myfin.by", MyfinBankParser("alfa-bank")),
            ])

        if parser_type == "tbank":
            return CompositeBankParser([
                ("T-Bank API", TBankDirectParser()),
                ("sravni.ru", SravniBankParser("t-bank")),
                ("ru.myfin.by", MyfinBankParser("t-bank")),
            ])

        if parser_type == "atb":
            return CompositeBankParser([
                ("ATB Direct", AtbDirectParser()),
                ("sravni.ru", SravniBankParser("aziatsko-tikhookeanskijj-bank")),
                ("ru.myfin.by", MyfinBankParser("aziatsko-tihookeanskiy-bank")),
                ("ru.myfin.by (atb)", MyfinBankParser("atb")),
            ])

        if parser_type in ("ingo_page", "ingobank"):
            return CompositeBankParser([
                ("Ingobank", IngobankPageParser()),
                ("ru.myfin.by", MyfinBankParser("ingobank")),
                ("sravni.ru", SravniBankParser("ingobank")),
                ("Ingobank Cache", IngobankParser(api_url or "", api_key)),
            ])

        if parser_type == "moex":
            return MoexParser()

        if parser_type == "cbrf":
            return CompositeBankParser([
                ("CBRF XML", CbrfParser()),
                ("CBRF JSON", CbrfJsonParser()),
            ])

        if parser_type == "cbrf_json":
            return CbrfJsonParser()

        if parser_type == "sravni_bank":
            return SravniBankParser(api_url)

        if parser_type == "sravni_agg":
            return SravniAggregatorParser()

        if parser_type == "myfin_ingo":
            return MyfinIngobankParser()

        return None