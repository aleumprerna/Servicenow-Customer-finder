from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz


LEGAL_SUFFIXES = {
    "ag",
    "bv",
    "co",
    "company",
    "corp",
    "corporation",
    "gmbh",
    "inc",
    "incorporated",
    "limited",
    "llc",
    "llp",
    "ltd",
    "nv",
    "plc",
    "private",
    "pte",
    "pty",
    "pvt",
    "sa",
    "sarl",
}


def normalize_company_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = text.replace("&", " and ")
    tokens = re.findall(r"[a-z0-9]+", text)
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def company_match_score(input_name: str, candidate_name: str) -> int:
    left = normalize_company_name(input_name)
    right = normalize_company_name(candidate_name)
    if not left or not right:
        return 0
    if left == right:
        return 100

    token_set = fuzz.token_set_ratio(left, right)
    weighted = fuzz.WRatio(left, right)
    score = round((token_set * 0.45) + (weighted * 0.55))

    # token_set_ratio alone makes "ABC" look identical to a much longer subsidiary.
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if left_tokens < right_tokens:
        extra_count = len(right_tokens - left_tokens)
        if len(left_tokens) == 1:
            score = min(score, 84 if extra_count == 1 else 78)
        elif extra_count >= 3:
            score = min(score, 84)
    return max(0, min(100, score))


@dataclass(frozen=True, slots=True)
class BestMatch:
    name: str
    score: int


def find_best_match(input_name: str, candidates: list[str] | tuple[str, ...]) -> BestMatch:
    best = BestMatch(name="", score=0)
    for candidate in candidates:
        score = company_match_score(input_name, candidate)
        if score > best.score:
            best = BestMatch(name=candidate, score=score)
    return best
