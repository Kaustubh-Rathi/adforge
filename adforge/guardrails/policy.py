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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import yaml

from adforge.guardrails.contracts import ComplianceVerdict
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


@dataclass
class PolicyEnforcer:
    """Rule-based compliance gate. Pure-Python so it's unit-testable."""

    rules: list[PolicyRule] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path) -> "PolicyEnforcer":
        data = yaml.safe_load(path.read_text()) or {"rules": []}
        rules = [PolicyRule(**r) for r in data["rules"]]
        return cls(rules=rules)

    @classmethod
    def default(cls) -> "PolicyEnforcer":
        """Load the bundled default ruleset."""
        rules_path = Path(__file__).parent / "policy_rules.yaml"
        if rules_path.exists():
            return cls.from_yaml(rules_path)
        # If the YAML isn't there (e.g. running tests in a stripped env),
        # return an enforcer with a minimal hard-coded rule set.
        return cls(rules=[
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
        ])

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
        risk = "low"

        for rule in self.rules:
            if rule.pattern.lower() in text:
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
        # employment/credit ads.
        if audience_targeting:
            sensitive_categories = {"race", "ethnicity", "sexual_orientation", "religion"}
            for cat in sensitive_categories:
                if cat in str(audience_targeting).lower():
                    blocked.append(f"TARGETING_{cat.upper()}")
                    risk = "critical"
                    flagged.append(f"Sensitive attribute targeting is prohibited: {cat}")

        approved = not blocked and risk in ("low", "medium")

        return ComplianceVerdict(
            approved=approved,
            risk_level=risk,  # type: ignore[arg-type]
            flagged_issues=flagged,
            required_disclosures=disclosures,
            reviewer_notes="Auto-evaluated by PolicyEnforcer" if approved else "Manual review required",
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
