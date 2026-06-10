"""Spam protection and input safety guard.

Two layers:
  1. **Rate limiter** — sliding-window, per-IP + global, blocks credit burn.
  2. **Input guard** — validates question length, detects common prompt-injection
     patterns, and rejects malicious or nonsensical payloads before they reach
     any LLM.

Both are pure-Python with no extra dependencies. For production deployments
behind a reverse proxy, read the real IP from X-Forwarded-For or a trusted
header (configure via RATE_LIMIT_TRUSTED_PROXIES env var).
"""

import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field

# ── Configuration (env-overridable) ──────────────────────────────────────────

# Per-IP limits
PER_IP_WINDOW = int(os.getenv("RATE_LIMIT_PER_IP_WINDOW", "60"))       # seconds
PER_IP_MAX = int(os.getenv("RATE_LIMIT_PER_IP_WINDOW_MAX", "6"))       # requests

# Global (all IPs combined) limits
GLOBAL_WINDOW = int(os.getenv("RATE_LIMIT_GLOBAL_WINDOW", "60"))        # seconds
GLOBAL_MAX = int(os.getenv("RATE_LIMIT_GLOBAL_WINDOW_MAX", "30"))       # requests

# Cooldown between consecutive requests from the same IP
MIN_INTERVAL = float(os.getenv("RATE_LIMIT_MIN_INTERVAL", "3"))         # seconds

# Input validation
MAX_QUESTION_LEN = int(os.getenv("GUARD_MAX_QUESTION_LEN", "1000"))     # characters
MIN_QUESTION_LEN = int(os.getenv("GUARD_MIN_QUESTION_LEN", "3"))        # characters

# ── Prompt-injection detection ───────────────────────────────────────────────
# These patterns catch the most common injection and jailbreak techniques
# without false-positiveing on normal questions.

_INJECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Direct instruction override
    (
        re.compile(
            r"(?i)\b(ignore\s+(all\s+)?previous|disregard\s+(all\s+)?previous|"
            r"forget\s+(all\s+)?(previous|above|prior))\b",
        ),
        "instruction override",
    ),
    # System prompt extraction / manipulation
    (
        re.compile(
            r"(?i)\b(system\s*prompt|you\s+are\s+now|new\s+instructions?|"
            r"override\s+(your|the)\s+(instructions?|rules?|system))\b",
        ),
        "system prompt manipulation",
    ),
    # Role-play jailbreak
    (
        re.compile(
            r"(?i)\b(pretend\s+you\s+(are|have|can|do|no\s+longer)|"
            r"act\s+as\s+if\s+you\s+(have\s+no|don'?t\s+have)\s+(rules|restrictions|filters))\b",
        ),
        "role-play jailbreak",
    ),
    # DAN / evil-confidant style
    (
        re.compile(
            r"(?i)\b(DAN\s+mode|evil\s+confidant|jailbreak|"
            r"developer\s+mode|god\s+mode|unrestricted)\b",
        ),
        "jailbreak pattern",
    ),
    # Output format manipulation (trying to get raw injections into LLM output)
    (
        re.compile(
            r"(?i)\b(output\s+the\s+(exact|following)\s+(text|string|word)s?\s*:?\s*"
            r"|repeat\s+(after\s+me|the\s+following)\b"
            r"|print\s+(exactly|the\s+following)\b)",
        ),
        "output manipulation",
    ),
    # Encoded payload attempts (base64, hex in quotes, unicode escapes)
    (
        re.compile(
            r"(?i)(?:base64|decode|\\x[0-9a-f]{2}|\\u[0-9a-f]{4}){3,}",
        ),
        "encoded payload",
    ),
    # Excessive repetition (spam indicator, not injection per se)
    (
        re.compile(r"(.{5,}?)\1{6,}"),
        "excessive repetition",
    ),
]


# ── Rate limiter ─────────────────────────────────────────────────────────────

@dataclass
class _WindowCounter:
    """Sliding-window rate counter."""
    window: float  # seconds
    max_count: int
    timestamps: list[float] = field(default_factory=list)

    def _trim(self, now: float) -> None:
        cutoff = now - self.window
        while self.timestamps and self.timestamps[0] <= cutoff:
            self.timestamps.pop(0)

    def check(self) -> bool:
        """Return True if the request is allowed."""
        now = time.monotonic()
        self._trim(now)
        return len(self.timestamps) < self.max_count

    def record(self) -> None:
        self.timestamps.append(time.monotonic())


@dataclass
class RateLimiter:
    """Per-IP + global sliding-window rate limiter."""

    per_ip: _WindowCounter = field(
        default_factory=lambda: _WindowCounter(PER_IP_WINDOW, PER_IP_MAX)
    )
    global_: _WindowCounter = field(
        default_factory=lambda: _WindowCounter(GLOBAL_WINDOW, GLOBAL_MAX)
    )
    _per_ip: dict[str, _WindowCounter] = field(default_factory=dict)
    _last_request: dict[str, float] = field(default_factory=dict)

    def check(self, ip: str) -> tuple[bool, str]:
        """Check if a request from *ip* is allowed.

        Returns (allowed, reason). When not allowed, *reason* explains why.
        """
        now = time.monotonic()

        # Global gate
        if not self.global_.check():
            return False, "Server is busy — please try again in a moment."

        # Per-IP counter (lazy create)
        counter = self._per_ip.get(ip)
        if counter is None:
            counter = _WindowCounter(PER_IP_WINDOW, PER_IP_MAX)
            self._per_ip[ip] = counter

        if not counter.check():
            return False, (
                f"Rate limit reached — max {PER_IP_MAX} questions "
                f"per {PER_IP_WINDOW}s."
            )

        # Minimum interval between consecutive requests
        last = self._last_request.get(ip, 0)
        if now - last < MIN_INTERVAL:
            wait = round(MIN_INTERVAL - (now - last), 1)
            return False, f"Slow down — try again in {wait}s."

        return True, ""

    def record(self, ip: str) -> None:
        """Record a successful (accepted) request."""
        counter = self._per_ip.get(ip)
        if counter:
            counter.record()
        self.global_.record()
        self._last_request[ip] = time.monotonic()

    def cleanup(self, max_age: float = 300) -> None:
        """Drop idle IP slots to prevent memory growth."""
        now = time.monotonic()
        idle = [
            ip for ip, t in self._last_request.items()
            if now - t > max_age
        ]
        for ip in idle:
            self._per_ip.pop(ip, None)
            self._last_request.pop(ip, None)


# Singleton limiter instance
limiter = RateLimiter()


def client_ip(request) -> str:
    """Extract the client IP from a FastAPI Request object.

    Respects X-Forwarded-For when RATE_LIMIT_TRUSTED_PROXIES is set.
    """
    trusted = os.getenv("RATE_LIMIT_TRUSTED_PROXIES", "")
    forwarded = request.headers.get("x-forwarded-for", "")
    if trusted and forwarded:
        # Take the rightmost IP set by the nearest trusted proxy
        parts = [p.strip() for p in forwarded.split(",")]
        proxies = {p.strip() for p in trusted.split(",")}
        for ip in reversed(parts):
            if ip not in proxies:
                return ip
    return request.client.host if request.client else "unknown"


# ── Input validation ─────────────────────────────────────────────────────────

class GuardError(Exception):
    """Raised when a question fails input validation."""
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


def validate_question(question: str) -> str:
    """Validate and sanitize a user question. Returns the cleaned question.

    Raises GuardError if the question is rejected.
    """
    # Basic presence check
    if not question or not question.strip():
        raise GuardError("Empty question", "Please ask something.")

    cleaned = question.strip()

    # Length checks
    if len(cleaned) < MIN_QUESTION_LEN:
        raise GuardError(
            "Too short",
            f"Question must be at least {MIN_QUESTION_LEN} characters.",
        )
    if len(cleaned) > MAX_QUESTION_LEN:
        raise GuardError(
            "Too long",
            f"Question must be under {MAX_QUESTION_LEN:,} characters. "
            f"Yours is {len(cleaned):,}.",
        )

    # Whitespace normalization
    cleaned = " ".join(cleaned.split())

    # Injection pattern scan
    for pattern, label in _INJECTION_PATTERNS:
        if pattern.search(cleaned):
            raise GuardError(
                "Potentially harmful input",
                f"Your question matched a known {label} pattern. "
                f"Please rephrase.",
            )

    # Null bytes and control characters (except newline/tab)
    if "\x00" in cleaned or any(
        ord(ch) < 0x20 and ch not in ("\n", "\t") for ch in cleaned
    ):
        raise GuardError("Invalid characters", "Control characters are not allowed.")

    return cleaned
