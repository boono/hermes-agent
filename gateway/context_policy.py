"""Domain-filtered context policy for gateway sessions.

This module is intentionally small and deterministic: it turns a platform
source + message + private identity policy into a decision about which of
the canonical user's top-level source domains may be injected for the turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from hermes_constants import get_hermes_home
from gateway.config import Platform
from gateway.session import SessionSource

TOP_DOMAINS = ("work", "life", "personal-research")
PLATFORM_LAYERS = ("wiki", "ops", "public")

_WORK_HINTS = (
    "工作", "项目", "客户", "公司", "会议", "需求", "排期", "上线", "部署", "代码",
    "bug", "pr", "repo", "github", "飞书", "企微", "wecom", "feishu", "project",
)
_LIFE_HINTS = (
    "伴侣", "partner", "family", "home", "家里", "家庭", "生活", "吃饭", "晚饭", "日常",
)
_PR_HINTS = (
    "奶茶", "喜茶", "霸王茶姬", "茶百道", "蜜雪", "品牌库", "菜单", "门店", "竞品",
    "研究", "个人研究", "ai", "投资", "写作", "判断框架",
)
_EXPLICIT_CROSS_DOMAIN_HINTS = (
    "明确要求", "切到", "聊一下", "说说", "总结", "同步", "跨平台", "previous chat",
)


@dataclass(frozen=True)
class ContextPolicyDecision:
    identity: str
    platform: str
    topic_domain: str
    inject_domains: list[str] = field(default_factory=list)
    proactive_external_domains: list[str] = field(default_factory=list)
    allowed_layers: list[str] = field(default_factory=list)
    can_read_cross_platform_context: bool = False
    explicit_cross_domain_request: bool = False


def _policy_path(hermes_home: Optional[str | Path]) -> Path:
    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    return home / "identity_policy.yaml"


def load_identity_policy(hermes_home: Optional[str | Path] = None) -> dict[str, Any]:
    path = _policy_path(hermes_home)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _platform_key(source: SessionSource) -> str:
    platform = source.platform
    if isinstance(platform, Platform):
        return platform.value
    return str(platform or "")


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(h.lower() in low for h in hints)


def classify_source_domain(message: str, source: Optional[SessionSource] = None) -> str:
    """Classify a turn into one of the configured source domains.

    Platform defaults are intentionally conservative: Feishu defaults to work
    unless the message clearly names a non-work topic, while Weixin relies more
    on content because it is a multi-domain entry.
    """

    text = str(message or "")
    if _contains_any(text, _LIFE_HINTS):
        return "life"
    if _contains_any(text, _PR_HINTS):
        return "personal-research"
    if _contains_any(text, _WORK_HINTS):
        return "work"
    if source is not None and _platform_key(source) == "feishu":
        return "work"
    return "work"


def _explicit_cross_domain_request(message: str) -> bool:
    return _contains_any(str(message or ""), _EXPLICIT_CROSS_DOMAIN_HINTS)


def _identity_config(policy: Mapping[str, Any], identity: str) -> dict[str, Any]:
    identities = policy.get("identities") if isinstance(policy, Mapping) else None
    if isinstance(identities, Mapping):
        cfg = identities.get(identity) or identities.get("unknown") or {}
        return dict(cfg) if isinstance(cfg, Mapping) else {}
    return {}


def _platform_config(policy: Mapping[str, Any], source: SessionSource) -> dict[str, Any]:
    defaults = policy.get("platform_defaults") if isinstance(policy, Mapping) else None
    if isinstance(defaults, Mapping):
        cfg = defaults.get(_platform_key(source)) or {}
        return dict(cfg) if isinstance(cfg, Mapping) else {}
    return {}


def _valid_domains(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(v) for v in values if str(v) in TOP_DOMAINS]


def _valid_layers(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(v) for v in values if str(v) in PLATFORM_LAYERS]


def _source_identifiers(source: SessionSource) -> set[str]:
    vals = {
        source.user_id,
        source.user_id_alt,
        source.user_name,
        f"{_platform_key(source)}:{source.user_id}" if source.user_id else None,
        f"{_platform_key(source)}:{source.user_id_alt}" if source.user_id_alt else None,
    }
    return {str(v).strip() for v in vals if str(v or "").strip()}


def resolve_canonical_identity(
    source: SessionSource,
    *,
    hermes_home: Optional[str | Path] = None,
    default: str = "unknown",
) -> str:
    """Resolve a platform source to a canonical identity.

    The private policy may contain aliases; raw IDs should stay in that private
    file and not in wiki. Unmapped sources default to ``unknown`` so new
    interfaces get public/current-session-only treatment until the owner confirms
    their identity/domain policy. A partner-name shortcut remains for channels
    that already provide a trusted display name.
    """

    identifiers = _source_identifiers(source)
    policy = load_identity_policy(hermes_home)
    identities = policy.get("identities") if isinstance(policy, Mapping) else None
    if isinstance(identities, Mapping):
        for identity, cfg in identities.items():
            if not isinstance(cfg, Mapping):
                continue
            aliases = cfg.get("aliases") or []
            if any(str(alias).strip() in identifiers for alias in aliases):
                return str(identity)
    return default


def resolve_context_policy(
    *,
    source: SessionSource,
    message: str,
    hermes_home: Optional[str | Path] = None,
    canonical_identity: str = "unknown",
) -> ContextPolicyDecision:
    policy = load_identity_policy(hermes_home)
    identity = canonical_identity or "unknown"
    topic_domain = classify_source_domain(message, source)
    identity_cfg = _identity_config(policy, identity)
    platform_cfg = _platform_config(policy, source)

    allowed_domains = _valid_domains(identity_cfg.get("allowed_domains"))
    allowed_layers = _valid_layers(identity_cfg.get("allowed_layers"))
    proactive = _valid_domains(platform_cfg.get("proactive_external_domains"))
    explicit = _explicit_cross_domain_request(message)

    inject_domains: list[str] = []
    if topic_domain in allowed_domains:
        inject_domains = [topic_domain]

    platform_default = platform_cfg.get("default_external_domain")
    if _platform_key(source) == "feishu" and not explicit:
        # Feishu may understand cross-platform context, but default external
        # behavior is work-only. Keep prompt injection aligned with that until
        # the canonical identity explicitly asks to use another domain in Feishu.
        if "work" in allowed_domains:
            inject_domains = ["work"]
        proactive = ["work"]
    elif isinstance(platform_default, str) and platform_default in TOP_DOMAINS:
        # For single-domain interfaces, prefer the platform default unless the
        # current message clearly chooses another allowed domain.
        if not inject_domains and platform_default in allowed_domains:
            inject_domains = [platform_default]

    return ContextPolicyDecision(
        identity=identity,
        platform=_platform_key(source),
        topic_domain=topic_domain,
        inject_domains=inject_domains,
        proactive_external_domains=proactive,
        allowed_layers=allowed_layers,
        can_read_cross_platform_context=bool(identity_cfg.get("can_read_cross_platform_context")),
        explicit_cross_domain_request=explicit,
    )


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            return text[end + 4 :].lstrip("\n")
    return text


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw_meta = text[4:end]
    body = text[end + 4 :].lstrip("\n")
    try:
        data = yaml.safe_load(raw_meta) or {}
    except Exception:
        data = {}
    return (data if isinstance(data, dict) else {}), body


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value in (None, "", "null"):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            # Date-only frontmatter expires at that UTC midnight. Prefer full
            # ISO timestamps for end-of-day semantics.
            try:
                dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _context_is_expired(meta: Mapping[str, Any], *, now: Optional[datetime] = None) -> bool:
    expires_at = _parse_datetime(meta.get("expires_at")) if isinstance(meta, Mapping) else None
    if expires_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return expires_at <= current


def _context_allows_identity(meta: Mapping[str, Any], identity: str) -> bool:
    if not isinstance(meta, Mapping):
        return True
    allowed = meta.get("allowed_identities")
    if not isinstance(allowed, list) or not allowed:
        return True
    return str(identity) in {str(v) for v in allowed}


def _context_domain_matches(meta: Mapping[str, Any], domain: str) -> bool:
    if not isinstance(meta, Mapping):
        return True
    declared = meta.get("domain")
    if declared in (None, ""):
        return True
    return str(declared) == domain


def _context_visibility(meta: Mapping[str, Any]) -> str:
    if not isinstance(meta, Mapping):
        return "unspecified"
    visibility = str(meta.get("visibility") or "unspecified")
    return visibility if visibility else "unspecified"


def render_domain_context_prompt(
    decision: ContextPolicyDecision,
    *,
    hermes_home: Optional[str | Path] = None,
    max_chars_per_domain: int = 4000,
) -> str:
    if not decision.inject_domains:
        return ""

    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    base = home / "active_context" / decision.identity
    blocks: list[str] = []
    for domain in decision.inject_domains:
        if domain not in TOP_DOMAINS:
            continue
        path = base / f"{domain}.md"
        if not path.exists():
            continue
        meta, body = _split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        if not _context_domain_matches(meta, domain):
            continue
        if _context_is_expired(meta):
            continue
        if not _context_allows_identity(meta, decision.identity):
            continue
        content = body.strip()
        if not content:
            continue
        if len(content) > max_chars_per_domain:
            content = content[:max_chars_per_domain].rstrip() + "\n…"
        visibility = _context_visibility(meta)
        blocks.append(f"## domain: {domain}\nvisibility: {visibility}\n\n{content}")

    if not blocks:
        return ""
    return (
        "[DOMAIN-FILTERED ACTIVE CONTEXT]\n"
        f"identity: {decision.identity}\n"
        f"platform: {decision.platform}\n"
        f"topic_domain: {decision.topic_domain}\n"
        "Only use this context within the listed domain(s). Do not surface other domains unless the user explicitly asks.\n\n"
        + "\n\n".join(blocks)
    )


def should_skip_builtin_memory(decision: ContextPolicyDecision) -> bool:
    """Return True when untagged global MEMORY/USER blocks must be withheld.

    Built-in MEMORY.md and USER.md are compact but not domain-tagged. Once a
    gateway turn is governed by a domain decision, injecting the unfiltered
    global blocks can leak facts from another domain; use filtered active_context
    instead.
    """

    if decision.identity == "unknown":
        return True
    if decision.platform == "feishu" and "work" in decision.proactive_external_domains:
        return True
    if decision.inject_domains and set(decision.inject_domains) != set(TOP_DOMAINS):
        return True
    return False


def render_memory_boundary_prompt(decision: ContextPolicyDecision) -> str:
    """Render a short, non-persistent boundary for domain-scoped gateway turns."""

    if not should_skip_builtin_memory(decision):
        return ""
    visible = ", ".join(decision.inject_domains or decision.allowed_layers or []) or "current session"
    blocked = [d for d in TOP_DOMAINS if d not in decision.inject_domains]
    blocked_text = ", ".join(blocked) if blocked else "none"
    return (
        "[DOMAIN MEMORY BOUNDARY]\n"
        "For this gateway turn, built-in global MEMORY/USER PROFILE is intentionally withheld because it is not domain-tagged.\n"
        f"Visible scope: {visible}.\n"
        f"Do not infer, list, or reveal facts from blocked domains: {blocked_text}.\n"
        "If the user asks what you remember, answer only for the visible scope and say other domains require an explicit domain switch."
    )


def build_domain_context_prompt(
    *,
    source: SessionSource,
    message: str,
    hermes_home: Optional[str | Path] = None,
    canonical_identity: Optional[str] = None,
) -> str:
    identity = canonical_identity or resolve_canonical_identity(source, hermes_home=hermes_home)
    decision = resolve_context_policy(
        source=source,
        message=message,
        hermes_home=hermes_home,
        canonical_identity=identity,
    )
    parts = [
        render_memory_boundary_prompt(decision),
        render_domain_context_prompt(decision, hermes_home=hermes_home),
    ]
    return "\n\n".join(p for p in parts if p)
