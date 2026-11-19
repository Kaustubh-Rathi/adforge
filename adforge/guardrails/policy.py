"""Compliance policy enforcer.

Runs creative + targeting against a set of rules BEFORE the campaign goes live.
The Compliance agent calls this; the orchestrator will not invoke the Media
Planner's "publish" tool unless ``PolicyEnforcer.approve()`` returns
``approved=True``.

Rules are loaded from ``adforge/guardrails/policy_rules.yaml`` (or a JSON
manifest in production) and are split into three tiers:

    tier 1 (hard block) — illegal / prohibited content (e.g. tobacco ads)
    tier 2 (require disclosure) — financial / health claims
    tier 3 (soft warning) — borderline phrasing, give agent a chance to revise

The enforcer is also responsible for platform-specific T&C checks (e.g. Meta's
prohibited personal attributes targeting, Google's prohibited content policy).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from adforge.guardrails.contracts import ComplianceVerdict, RiskLevel
from adforge.guardrails.exceptions import PolicyViolation


@dataclass
class PolicyRule:
    """A single compliance rule.

    rule_id       — stable identifier surfaced in audit logs
    tier          — 1=hard_block, 2=require_disclosure, 3=soft_warning
    pattern       — substring or regex pattern to match against creative text
    description   — human-readable rationale
    required_disclosure — if tier 2, the disclosure text that must be appended
    """

    rule_id: str
    tier: int  # 1, 2, 3
    pattern: str
    description: str
    required_disclosure: str = ""


_EXCLUSION_KEY_MARKERS = (
    "exclude",
    "exclusion",
    "negative",
    "block",
    "avoid",
    "do_not",
    "do not",
    "not_",
)

# Media plans carry prose fields that *describe* targeting policy rather than
# perform it, e.g. ``constraint_notes`` and ``sensitive_attribute_policy``.
# Scanning those as targeting turns "no targeting on race or religion" into a
# false hard block. Skip the whole subtree for keys that signal commentary.
_DESCRIPTIVE_KEY_MARKERS = (
    "note",
    "policy",
    "comment",
    "description",
    "summary",
    "rationale",
    "constraint",
    "guideline",
    "disclaimer",
    "explanation",
    "justification",
    "assumption",
    "context",
    "review",
    "check",
)


def _collect_targeting_terms(value: Any, *, excluded: bool = False) -> list[str]:
    """Flatten positive audience-targeting strings, skipping non-targeting data.

    Media plans legitimately contain entries such as
    ``{"exclusions": ["race", "religion"]}`` or
    ``{"sensitive_attribute_policy": "No targeting on race or religion"}``.
    A naive keyword scan treats those as targeting and hard-blocks a compliant
    campaign. Only positive targeting values are returned here; exclusion
    subtrees and descriptive/commentary fields are ignored.
    """
    terms: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            child_skipped = excluded or any(
                marker in key_text
                for marker in (*_EXCLUSION_KEY_MARKERS, *_DESCRIPTIVE_KEY_MARKERS)
            )
            if not child_skipped:
                terms.append(key_text)
            terms.extend(_collect_targeting_terms(item, excluded=child_skipped))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            terms.extend(_collect_targeting_terms(item, excluded=excluded))
    elif isinstance(value, str) and not excluded:
        terms.append(value.lower())
    return terms


@dataclass
class PolicyEnforcer:
    """Rule-based compliance gate. Pure-Python so it's unit-testable."""

    rules: list[PolicyRule] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path) -> PolicyEnforcer:
        data = yaml.safe_load(path.read_text()) or {"rules": []}
        rules = [PolicyRule(**r) for r in data["rules"]]
        return cls(rules=rules)

    @classmethod
    def default(cls) -> PolicyEnforcer:
        """Load the bundled default ruleset."""
        rules_path = Path(__file__).parent / "policy_rules.yaml"
        if rules_path.exists():
            return cls.from_yaml(rules_path)
        # If the YAML isn't there (e.g. running tests in a stripped env),
        # return an enforcer with a minimal hard-coded rule set.
        return cls(
            rules=[
                PolicyRule(
                    rule_id="BLOCK_TOBACCO",
                    tier=1,
                    pattern="tobacco|cigarette|vape",
                    description="Tobacco / nicotine advertising is prohibited on all major ad platforms.",
                ),
                PolicyRule(
                    rule_id="DISCLOSE_FINANCIAL",
                    tier=2,
                    pattern="guaranteed return|risk-free investment|crypto|get rich",
                    description="Financial claims require disclosure language per FTC + platform policy.",
                    required_disclosure="Investing involves risk. Past performance is not indicative of future results.",
                ),
                PolicyRule(
                    rule_id="WARN_HEALTH",
                    tier=3,
                    pattern="cure|miracle|guaranteed weight loss",
                    description="Health claims require substantiation. Review with legal counsel before launch.",
                ),
            ]
        )

    def evaluate(
        self,
        creative_text: str,
        audience_targeting: dict[str, Any] | None = None,
        channel: str | None = None,
    ) -> ComplianceVerdict:
        """Run creative text against the ruleset. Returns a typed verdict."""
        text = creative_text.lower()
        flagged: list[str] = []
        disclosures: list[str] = []
        blocked: list[str] = []
        risk: RiskLevel = "low"

        for rule in self.rules:
            # Rules are regex alternations (e.g. ``tobacco|cigarette|vape``).
            # A plain substring check would never match them.
            try:
                matched = re.search(rule.pattern, text, flags=re.IGNORECASE) is not None
            except re.error:
                matched = rule.pattern.lower() in text
            if matched:
                if rule.tier == 1:
                    blocked.append(rule.rule_id)
                    risk = "critical"
                    flagged.append(f"{rule.rule_id}: {rule.description}")
                elif rule.tier == 2:
                    if rule.required_disclosure:
                        disclosures.append(rule.required_disclosure)
                    risk = "high" if risk != "critical" else risk
                    flagged.append(f"{rule.rule_id}: {rule.description}")
                elif rule.tier == 3:
                    risk = "medium" if risk == "low" else risk
                    flagged.append(f"{rule.rule_id}: {rule.description}")

        # Audience targeting policy check — Meta prohibits targeting on
        # personal attributes (race, sexual orientation, etc.) for housing/
        # employment/credit ads. Only positive targeting terms are inspected;
        # explicit exclusion lists must not trip the hard block.
        if audience_targeting:
            sensitive_categories = {"race", "ethnicity", "sexual_orientation", "religion"}
            targeting_text = " ".join(_collect_targeting_terms(audience_targeting))
            for cat in sensitive_categories:
                if re.search(rf"\b{re.escape(cat)}\b", targeting_text):
                    blocked.append(f"TARGETING_{cat.upper()}")
                    risk = "critical"
                    flagged.append(f"Sensitive attribute targeting is prohibited: {cat}")

        # Tier 2 (disclosure) and tier 3 (soft warning) remain approvable; only
        # hard blocks make the creative unpublishable. The required disclosures
        # are carried in the verdict for the publish step to append.
        approved = not blocked and risk != "critical"

        return ComplianceVerdict(
            approved=approved,
            risk_level=risk,
            flagged_issues=flagged,
            required_disclosures=disclosures,
            reviewer_notes="Auto-evaluated by PolicyEnforcer"
            if approved
            else "Manual review required",
            blocked_terms=blocked,
        )

    def assert_approved(
        self,
        creative_text: str,
        audience_targeting: dict[str, Any] | None = None,
        channel: str | None = None,
    ) -> ComplianceVerdict:
        """Like evaluate(), but raises PolicyViolation when not approved.

        Used by the Media Planner's "publish" tool path — if the enforcer
        raises, the publish is blocked and the orchestrator routes the
        creative back to the Creative agent for revision.
        """
        verdict = self.evaluate(creative_text, audience_targeting, channel)
        if not verdict.approved:
            raise PolicyViolation(
                rule_id=",".join(verdict.blocked_terms) or "COMPLIANCE_GATE",
                detail=f"Creative blocked. Risk: {verdict.risk_level}. Issues: {verdict.flagged_issues}",
            )
        return verdict
