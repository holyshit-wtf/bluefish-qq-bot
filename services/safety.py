from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

import jieba


_HTML_TAG = re.compile(r"<[^>]{0,200}>")
_FENCED_CODE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_MARKDOWN_LINK = re.compile(r"!?\[([^\]]{0,100})\]\([^)]{0,300}\)")
_URL = re.compile(
    r"(?:https?://|www\.)[^\s，。！？；：、<>()（）]{1,300}",
    re.IGNORECASE,
)
_BARE_DOMAIN = re.compile(
    r"(?<![A-Za-z0-9_-])(?:[A-Za-z0-9-]+\.)+"
    r"(?:com|cn|net|org|io|me|cc|top|xyz)(?:/[^\s，。！？；：、<>()（）]*)?",
    re.IGNORECASE,
)
_MENTION = re.compile(r"[@＠](?:全体成员|所有人|[^\s，。！？,.!?]{1,32})")
_MARKDOWN_MARKS = re.compile(r"(?m)^\s{0,3}(?:#{1,6}\s+|[-*+]\s+|\d+\.\s+)")
_FORMAT_MARKS = re.compile(r"[*_~`>|]+")
_CONTACT = re.compile(
    r"(?:QQ群?|群号|微信|V信|vx|wx|电话|手机)"
    r"\s*[:：]?\s*[A-Za-z0-9_-]{5,20}",
    re.IGNORECASE,
)
_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", "", normalized)


def normalize_blocked_term(value: str) -> str:
    return normalize_text(value)


def blocked_term_for(content: str, terms: Iterable[str]) -> str | None:
    normalized = normalize_text(content)
    if not normalized:
        return None
    for term in terms:
        if term and term in normalized:
            return term
    return None


def search_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in jieba.cut(value.strip(), cut_all=False):
        token = raw.strip()
        if not token or all(character.isspace() for character in token):
            continue
        if len(token) == 1 and not token.isalnum():
            continue
        folded = token.casefold()
        if folded not in seen:
            seen.add(folded)
            tokens.append(token)
    return tokens[:50]


def search_text(value: str) -> str:
    return " ".join(search_tokens(value))


def fts_query(value: str) -> str:
    tokens = search_tokens(value)
    quoted = []
    for token in tokens:
        escaped = token.replace('"', '""')
        quoted.append(f'"{escaped}"')
    return " OR ".join(quoted)


@dataclass(frozen=True, slots=True)
class SafetyResult:
    allowed: bool
    matched_rule: str | None = None


def check_group_message(
    content: str, blocked_terms: Iterable[str]
) -> SafetyResult:
    matched = blocked_term_for(content, blocked_terms)
    return SafetyResult(allowed=matched is None, matched_rule=matched)


def sanitize_reply(value: str, max_length: int = 300) -> str:
    text = (value or "").strip()
    text = html.unescape(text)
    text = _FENCED_CODE.sub("", text)
    text = _HTML_TAG.sub("", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _BARE_DOMAIN.sub("", text)
    text = _MENTION.sub("", text)
    text = _CONTACT.sub("", text)
    text = _MOBILE.sub("", text)
    text = _MARKDOWN_MARKS.sub("", text)
    text = _FORMAT_MARKS.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return text


def is_safe_outbound(value: str, max_length: int = 300) -> bool:
    if not value or len(value) > max_length:
        return False
    normalized = unicodedata.normalize("NFKC", value)
    if _URL.search(normalized):
        return False
    if _HTML_TAG.search(normalized):
        return False
    if "@全体成员" in normalized or "@所有人" in normalized:
        return False
    return True

