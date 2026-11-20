import re
import difflib
from typing import Optional


class TextMatcher:
    """
    Advanced text matcher for handling user typos and abbreviations.
    """
    ALIASES = {
        "сбер": "сбербанк",
        "тбанк": "тбанк",
        "т-банк": "тбанк",
        "тинькофф": "тбанк",
        "альфа": "альфабанк",
        "втб": "втб",
        "атб": "атб",
        "азиатскотихоокеанский": "атб",
        "инго": "инго",
        "ингобанк": "инго",
        "мосбиржа": "мосбиржа",
        "цб": "цбрф",
        "цбрф": "цбрф",
    }

    @classmethod
    def _normalize(cls, text: str) -> str:
        text = text.lower()
        text = re.sub(
            r"(ооо|пао|публичное|акционерное|общество|банк|банковский|пб|кб|ао)",
            "",
            text,
        )
        text = re.sub(r"[\s\-_\.]", "", text)
        return text

    @classmethod
    def find_best_match(
        cls, query: str, choices: list[str], threshold: float = 0.75
    ) -> Optional[str]:
        if not query or not choices:
            return None

        normalized_query = cls._normalize(query)
        if normalized_query in cls.ALIASES:
            normalized_query = cls.ALIASES[normalized_query]

        normalized_choices = {cls._normalize(c): c for c in choices}

        if normalized_query in normalized_choices:
            return normalized_choices[normalized_query]

        best_match = None
        highest_ratio = 0.0

        for norm_choice, original_choice in normalized_choices.items():
            ratio = difflib.SequenceMatcher(None, normalized_query, norm_choice).ratio()
            if ratio > highest_ratio and ratio >= threshold:
                highest_ratio = ratio
                best_match = original_choice

        return best_match