"""Deterministic, zero-token activity analyzer for the Tips Kit (Phase T1).

Reads the user's local daily history summaries and detects behavioural
patterns that an existing KiroCrew capability would streamline, emitting
``CandidateRec`` records. There is **no LLM call and no network** — pure text
pattern matching over already-redacted local memory — so the analyzer adds
zero token cost and runs in milliseconds. It is the "background analyzer" the
Tips Kit RFC (docs/request-for-change/rfc-tips-kit.md) specifies; the LLM
phrasing/ranking layer described there is a later phase.

Two detector families ship in T1:

* **manual workflow -> built-in feature** — the educational ``feature_tip``
  kind: surface a capability (``/goal``, ``/compact``, sub-agents, monitoring)
  the user isn't using but whose manual equivalent shows up in their activity.
  No side effect; it navigates / drops a ready-to-send prompt.
* **recurring task -> scheduled cron** (``cron_create``) — recurring-cadence
  language ("every morning", "daily") that no existing cron already covers,
  surfaced via a ready-to-send ``cta_prompt`` the user sends to create the job.
  (The one-click confirm UI for this kind is a later phase; T1 routes through
  the ordinary consent path — the user sends the message.)

Candidates render into ordinary 7-field tip dicts via :func:`candidate_to_tip`
with STABLE ids (``analyzer-<family>``). Because the id is deterministic (not
LLM-invented), the existing id-keyed dismiss/snooze/shown suppression in
``TipsState`` works directly — no doc-stable-identity machinery is needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Kind constants (subset of the RFC's kind set that T1 implements).
KIND_FEATURE_TIP = "feature_tip"
KIND_CRON_CREATE = "cron_create"

# Stable id prefix for analyzer-produced tips (see module docstring).
ANALYZER_ID_PREFIX = "analyzer-"

_MAX_SNIPPET = 120


@dataclass(frozen=True)
class CandidateRec:
    """A single activity-grounded recommendation candidate.

    ``family`` is the stable weighting/suppression key; the rendered tip id is
    ``analyzer-<family>``. ``strength`` is the detector's confidence in [0, 1].
    """

    kind: str
    family: str
    feature: str
    title: str
    body: str
    why: str
    cta_prompt: str
    strength: float
    doc: str = ""


@dataclass(frozen=True)
class _FeatureRule:
    """A manual-workflow -> built-in-feature detector.

    Fires when ``signal_patterns`` match at least ``min_hits`` times AND no
    ``usage_patterns`` match (the user is not already using the feature).
    """

    family: str
    feature: str
    title: str
    body: str
    cta_prompt: str
    signal_patterns: tuple[str, ...]
    usage_patterns: tuple[str, ...]
    min_hits: int = 2
    doc: str = ""
    _signal_res: tuple[re.Pattern[str], ...] = field(default=(), compare=False, repr=False)
    _usage_res: tuple[re.Pattern[str], ...] = field(default=(), compare=False, repr=False)

    def compiled_signals(self) -> tuple[re.Pattern[str], ...]:
        return self._signal_res or tuple(re.compile(p, re.IGNORECASE) for p in self.signal_patterns)

    def compiled_usage(self) -> tuple[re.Pattern[str], ...]:
        return self._usage_res or tuple(re.compile(p, re.IGNORECASE) for p in self.usage_patterns)


# ── Feature detectors (manual workflow -> built-in capability) ──
#
# Tone matches the tips doc-register: plain statement of what the feature does
# and when it helps. No hype, no emoji, no exclamation marks.

_FEATURE_RULES: tuple[_FeatureRule, ...] = (
    _FeatureRule(
        family="goal",
        feature="Goal mode",
        title="Run long multi-step tasks with /goal",
        body=(
            "For a task that spans many steps, `/goal <objective>` lets the agent plan and "
            "work through it on its own instead of you driving each step."
        ),
        cta_prompt="/goal ",
        signal_patterns=(
            r"step by step",
            r"one step at a time",
            r"\bnext step\b",
            r"multi[- ]?step",
            r"\bstep \d",
            r"long[- ]running task",
        ),
        usage_patterns=(r"/goal\b", r"goal[- ]loop"),
        min_hits=2,
    ),
    _FeatureRule(
        family="subagents",
        feature="Sub-agents",
        title="Spawn sub-agents for independent parallel work",
        body=(
            "When several pieces of work are independent, sub-agents run them in parallel and "
            "report back, instead of working through them one at a time."
        ),
        cta_prompt="Spawn sub-agents to do these in parallel: ",
        signal_patterns=(
            r"one at a time",
            r"\bin parallel\b",
            r"at the same time",
            r"for each .*(then|and)",
            r"several (tasks|files|things|items)",
        ),
        usage_patterns=(r"\bspawn\b", r"sub[- ]?agent"),
        min_hits=2,
    ),
    _FeatureRule(
        family="compact",
        feature="Compact",
        title="Use /compact when a session grows large",
        body=(
            "If a long session starts feeling slow or loses earlier context, `/compact` "
            "summarises the history so the agent stays fast and on-track."
        ),
        cta_prompt="/compact",
        signal_patterns=(
            r"getting slow",
            r"context (is )?(getting )?(full|large|long)",
            r"losing (the )?context",
            r"forgot(ten)? (what|the|about)",
        ),
        usage_patterns=(r"/compact\b",),
        min_hits=1,
    ),
    _FeatureRule(
        family="monitor",
        feature="Monitoring",
        title="Watch something until it changes with monitoring",
        body=(
            "For \u201ctell me when X happens\u201d, a monitoring loop re-checks on an interval "
            "and only pings you when the condition is met, instead of you polling by hand."
        ),
        cta_prompt="Keep checking and let me know when ",
        signal_patterns=(
            r"keep checking",
            r"let me know when",
            r"check (it |this )?every",
            r"\bbabysit\b",
            r"watch (the|this|for|until)",
        ),
        usage_patterns=(r"monitor_start", r"\bmonitoring\b"),
        min_hits=1,
    ),
)


# ── Recurring-cadence detector (recurring task -> cron) ──

_CADENCE_RE = re.compile(
    r"\b(?:"
    r"every (?:morning|day|weekday|week|night|hour|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"\d+ (?:hours?|minutes?|days?))"
    r"|each (?:morning|day|week|night)"
    r"|daily|weekly|nightly"
    r")\b",
    re.IGNORECASE,
)

# Lines that are already about scheduling are not opportunities to suggest it.
_ALREADY_CRON_RE = re.compile(r"\b(cron|cron_add|scheduled? (a )?(job|task)|cron job)\b", re.IGNORECASE)

# "nightly"/"daily" are also PRODUCT nouns in this codebase (nightly build, daily
# release, nightly channel/feed). Those describe software, not a user habit, and
# were the entire false-positive set when the detector was probed against real
# history. Reject the cadence word when it sits in a product-noun context.
_PRODUCT_CADENCE_RE = re.compile(
    r"\b(?:nightly|daily|weekly)\s+"
    r"(?:build|builds|release|releases|feed|feeds|channel|channels|"
    r"artifact|artifacts|version|versions|run|runs|job|jobs|workflow|workflows|"
    r"tag|tags|publish|stamp|alias|image|images|snapshot|snapshots)\b"
    r"|\b(?:build|release|feed|channel|publish|workflow)\s+(?:nightly|daily|weekly)\b",
    re.IGNORECASE,
)

# A recurring task worth scheduling is something the USER does. Require
# first-person/imperative habit framing so third-person narration about the
# product ("the nightly stamps the version") can't become a candidate.
_HABIT_SUBJECT_RE = re.compile(
    r"\b(?:i|we|my|me)\b|\b(?:remind|summar\w+|check|review|pull|report|digest|briefing)\b",
    re.IGNORECASE,
)

_MAX_CRON_CANDIDATES = 2

_STOPWORDS = frozenset(
    {"every", "daily", "weekly", "nightly", "morning", "check", "about", "which", "there", "their"}
)

# Leading timestamp / narration preamble in daily-history lines, e.g.
# "On 2026-07-16 at 22:40-22:54 PDT, Zezhen ..." — stripped before slugging so
# family ids are derived from the ACTIVITY, not the date (a date-derived slug is
# unstable by construction: the same habit gets a new id every day).
_HISTORY_PREAMBLE_RE = re.compile(
    r"^(?:on\s+)?\d{4}-\d{2}-\d{2}(?:\s+(?:at\s+)?[\d:apm\u2013\u2014-]+)?"
    r"(?:\s+[A-Z]{2,5})?\s*,?\s*",
    re.IGNORECASE,
)


def _slug(text: str, limit: int = 40) -> str:
    """Deterministic lowercase alnum/hyphen slug for stable family ids."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit] or "task"


def _clean_snippet(line: str) -> str:
    snippet = " ".join(line.split()).strip("-*# ").strip()
    if len(snippet) > _MAX_SNIPPET:
        snippet = snippet[:_MAX_SNIPPET].rstrip() + "\u2026"
    return snippet


def _detect_recurring(
    history_text: str,
    existing_crons: str,
    dismissed_families: frozenset[str],
) -> list[CandidateRec]:
    """Emit cron_create candidates for recurring-cadence lines not already scheduled.

    **Gated off by default** (``analyze_activity(enable_cron=False)``).

    Why: this is a line-level matcher, but "recurring" is a CROSS-DAY property.
    The daily-history corpus is LLM narrative prose in which one line summarises
    an entire session, so cadence words and habit words co-occur incidentally and
    a single line cannot establish that the same intent recurred on N distinct
    days. Probed against 14 days of real history the detector produced only
    false positives (product-noun cadence like "nightly build", then narrative
    lines that merely contained both a cadence and a habit verb); two rounds of
    pattern tightening reduced but did not eliminate them.

    The fix is a better input, not a better regex: a structured per-session
    activity-signal record (recurring verbatim intents with timestamps) makes
    this detector trivially precise. Code and tests are retained unchanged so it
    can be enabled the day that source exists.
    """
    out: list[CandidateRec] = []
    seen: set[str] = set()
    existing_lower = existing_crons.lower()
    for raw_line in history_text.splitlines():
        if not _CADENCE_RE.search(raw_line):
            continue
        if _ALREADY_CRON_RE.search(raw_line):
            continue
        # Product-noun cadence ("nightly build") is about software, not a habit.
        if _PRODUCT_CADENCE_RE.search(raw_line):
            continue
        # Require the line to describe something the USER does.
        if not _HABIT_SUBJECT_RE.search(raw_line):
            continue
        snippet = _clean_snippet(raw_line)
        if len(snippet) < 12:
            continue
        # Slug from the activity, not the date preamble: a date-derived family id
        # changes every day and would defeat dismiss/snooze suppression entirely.
        slug_src = _HISTORY_PREAMBLE_RE.sub("", snippet)
        family = "cron-" + _slug(slug_src or snippet)
        if family in seen or family in dismissed_families:
            continue
        # Suppress if an existing cron already looks like it covers this line:
        # any word >=5 chars from the snippet appearing in a cron name/message.
        words = [w for w in re.findall(r"[a-z]{5,}", snippet.lower()) if w not in _STOPWORDS]
        if words and sum(1 for w in words if w in existing_lower) >= 2:
            continue
        seen.add(family)
        out.append(
            CandidateRec(
                kind=KIND_CRON_CREATE,
                family=family,
                feature="Scheduled jobs",
                title="Schedule a recurring task",
                body=(
                    "This looks like something you do on a schedule. A cron job can run it "
                    f"automatically: \u201c{snippet}\u201d."
                ),
                why="Recurring-cadence wording appeared in your recent activity.",
                cta_prompt=f"Set up a recurring cron job for: {snippet}",
                strength=0.6,
            )
        )
        if len(out) >= _MAX_CRON_CANDIDATES:
            break
    return out


def analyze_activity(
    history_text: str,
    *,
    existing_features: frozenset[str] = frozenset(),
    existing_crons: str = "",
    dismissed_families: frozenset[str] = frozenset(),
    max_candidates: int = 6,
    enable_cron: bool = False,
) -> list[CandidateRec]:
    """Detect activity-grounded recommendation candidates. Pure and deterministic.

    Args:
        history_text: concatenated recent daily history (already redacted).
        existing_features: family keys the user already uses (extra suppression
            on top of each rule's own usage patterns).
        existing_crons: a text blob of existing cron names/messages, used to
            suppress cron candidates already covered.
        dismissed_families: family keys the user has dismissed (never re-emit).
        max_candidates: hard cap on returned candidates.
        enable_cron: opt into the recurring-cadence -> cron detector. **Default
            off**, see :func:`_detect_recurring` for why: it cannot reach
            acceptable precision against narrative daily-history prose. Kept
            switchable so it can be turned on unchanged once a structured
            per-session activity-signal source exists.

    Returns:
        Candidates sorted by descending strength, capped at ``max_candidates``.
    """
    text = history_text or ""
    out: list[CandidateRec] = []

    for rule in _FEATURE_RULES:
        if rule.family in dismissed_families or rule.family in existing_features:
            continue
        if any(r.search(text) for r in rule.compiled_usage()):
            continue  # already uses the feature
        hits = sum(len(r.findall(text)) for r in rule.compiled_signals())
        if hits < rule.min_hits:
            continue
        # Confidence saturates at 2x the threshold.
        strength = min(1.0, hits / (rule.min_hits * 2))
        out.append(
            CandidateRec(
                kind=KIND_FEATURE_TIP,
                family=rule.family,
                feature=rule.feature,
                title=rule.title,
                body=rule.body,
                why="Based on how you've been working recently.",
                cta_prompt=rule.cta_prompt,
                strength=strength,
                doc=rule.doc,
            )
        )

    if enable_cron:
        out.extend(_detect_recurring(text, existing_crons, dismissed_families))

    out.sort(key=lambda c: c.strength, reverse=True)
    return out[:max_candidates]


def candidate_to_tip(rec: CandidateRec) -> dict:  # type: ignore[type-arg]
    """Render a candidate into the 7-field tip dict with a STABLE id.

    The id is ``analyzer-<family>`` so the existing id-keyed suppression
    (dismiss/snooze/shown) in TipsState applies without doc-stable machinery.
    """
    return {
        "id": ANALYZER_ID_PREFIX + rec.family,
        "feature": rec.feature,
        "title": rec.title,
        "body": rec.body,
        "why": rec.why,
        "doc": rec.doc,
        "cta_prompt": rec.cta_prompt,
    }


def dismissed_families_from_ids(dismissed_ids: list[str]) -> frozenset[str]:
    """Extract analyzer family keys from a list of dismissed tip ids."""
    return frozenset(
        tid[len(ANALYZER_ID_PREFIX):]
        for tid in dismissed_ids
        if tid.startswith(ANALYZER_ID_PREFIX)
    )
