from __future__ import annotations

import fnmatch
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.core.config import settings

DEFAULT_BUCKET = "neutral"


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    exe: tuple[str, ...] = ()
    title_any: tuple[str, ...] = ()
    title_all: tuple[str, ...] = ()
    title_not: tuple[str, ...] = ()
    # Marks a rule that is a guess rather than a fact. A browser window is the
    # usual case: the process name says nothing, and the title may or may not
    # give it away. Ambiguous matches are handed to the classifier for a
    # second opinion instead of being trusted outright.
    ambiguous: bool = False

    def matches(self, exe: str, title: str) -> bool:
        if self.exe and not any(fnmatch.fnmatchcase(exe, pat) for pat in self.exe):
            return False
        if self.title_not and any(kw in title for kw in self.title_not):
            return False
        if self.title_all and not all(kw in title for kw in self.title_all):
            return False
        if self.title_any and not any(kw in title for kw in self.title_any):
            return False
        # A rule with no title constraints matches on exe alone; a rule with no
        # exe constraint matches on title alone. A rule with neither is inert.
        return bool(self.exe or self.title_any or self.title_all)


@dataclass
class Category:
    key: str
    label: str
    bucket: str
    color: str


@dataclass
class RuleSet:
    categories: dict[str, Category] = field(default_factory=dict)
    rules: list[Rule] = field(default_factory=list)
    fallback: str = "uncategorized"

    def categorize(self, exe: str, title: str) -> tuple[str, str, str, bool]:
        """-> (category, bucket, rule_id, ambiguous). Case-insensitive."""
        exe_l = (exe or "").lower()
        title_l = (title or "").lower()
        for rule in self.rules:
            if rule.matches(exe_l, title_l):
                return rule.category, self.bucket_of(rule.category), rule.id, rule.ambiguous
        # Nothing matched at all, which is the most uncertain outcome there is.
        return self.fallback, self.bucket_of(self.fallback), "", True

    def bucket_of(self, category: str) -> str:
        cat = self.categories.get(category)
        return cat.bucket if cat else DEFAULT_BUCKET


def _as_tuple(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    return tuple(str(v).lower() for v in value)


def load_ruleset(path: Path | None = None) -> RuleSet:
    path = path or settings.rules_path
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    categories: dict[str, Category] = {}
    for key, meta in (raw.get("categories") or {}).items():
        meta = meta or {}
        categories[key] = Category(
            key=key,
            label=meta.get("label", key.title()),
            bucket=meta.get("bucket", DEFAULT_BUCKET),
            color=meta.get("color", "#64748b"),
        )

    fallback = raw.get("fallback", "uncategorized")
    if fallback not in categories:
        categories[fallback] = Category(fallback, "Uncategorized", DEFAULT_BUCKET, "#475569")

    rules: list[Rule] = []
    for i, item in enumerate(raw.get("rules") or []):
        category = item.get("category")
        if category not in categories:
            # Skip rather than crash: a typo in one rule should not take the
            # whole tracker down.
            continue
        rules.append(
            Rule(
                id=str(item.get("id") or f"rule-{i}"),
                category=category,
                exe=_as_tuple(item.get("exe")),
                title_any=_as_tuple(item.get("title_any")),
                title_all=_as_tuple(item.get("title_all")),
                title_not=_as_tuple(item.get("title_not")),
                ambiguous=bool(item.get("ambiguous", False)),
            )
        )

    return RuleSet(categories=categories, rules=rules, fallback=fallback)


class _Holder:
    """Hot-reloadable singleton, so editing rules.yml does not need a restart."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ruleset: RuleSet | None = None
        self.error: str = ""

    def get(self) -> RuleSet:
        if self._ruleset is None:
            self.reload()
        return self._ruleset  # type: ignore[return-value]

    def reload(self) -> RuleSet:
        with self._lock:
            try:
                self._ruleset = load_ruleset()
                self.error = ""
            except Exception as exc:  # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"
                if self._ruleset is None:
                    # Never leave the app without a ruleset.
                    self._ruleset = RuleSet(
                        categories={
                            "uncategorized": Category(
                                "uncategorized", "Uncategorized", DEFAULT_BUCKET, "#475569"
                            )
                        }
                    )
            return self._ruleset


ruleset = _Holder()
