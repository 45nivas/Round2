"""Core data models, including the single shared Cohort abstraction (C2)."""
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Cohort:
    """Single shared cohort object used across detection, experiments, schedule, and supervisor view (C2)."""
    id: str
    where: dict[str, list[str]]

    def matches(self, features: dict[str, str]) -> bool:
        """AND across features in `where`, OR within each feature's allowed list."""
        for feat_key, allowed_values in self.where.items():
            val = features.get(feat_key)
            if val is None or val not in allowed_values:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "where": {k: list(v) for k, v in sorted(self.where.items())}}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Cohort":
        return cls(
            id=str(data["id"]),
            where={str(k): [str(x) for x in v] for k, v in data.get("where", {}).items()},
        )


def assign_cohort(cohorts: list[Cohort], features: dict[str, str]) -> str:
    """Assign a lead's feature dictionary to the first matching cohort ID, or 'default'."""
    for c in cohorts:
        if c.matches(features):
            return c.id
    return "default"


@dataclass
class ProposalPackage:
    """Packaged config change ready for governance routing (C9, C10)."""
    proposal_id: str
    workflow_id: str
    tenant_id: str
    base_version: int
    proposed_config: dict[str, Any]
    changed_dims: list[str]
    change_summary: str
    diff: dict[str, Any]
    affected_leads: int
    dry_run: dict[str, Any]
    impact_score: float
    rationale: str
    evidence: dict[str, Any] | None = None
    is_urgent_incident: bool = False
    expires_in_hours: int | None = None


@dataclass
class IncidentRecord:
    """Tracks an operational incident (hallucination spike, DID degradation, or variant decay)."""
    incident_id: str
    workflow_id: str
    tenant_id: str
    incident_type: str  # 'hallucination_spike' | 'did_degraded' | 'variant_decay' | 'supervisor_edit'
    detected_at_minute: int
    detected_at_ts: str
    status: str         # 'open' | 'mitigating' | 'verifying' | 'resolved'
    details: dict[str, Any] = field(default_factory=dict)
    mitigation_proposal_id: str | None = None
    resolved_at_minute: int | None = None
    resolved_at_ts: str | None = None
    verification_summary: str | None = None
