"""Shared, immutable CVM configuration for CLI, Web and SDK callers."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from typing import Literal

CvmMode = Literal["off", "shadow", "active"]


@dataclass(frozen=True, slots=True)
class CvmConfig:
    """Contract modes and passive stage-2 evidence; other components are reserved.

    Rejecting unsupported modes prevents a configured-but-inoperative gate.
    Later stages must add behavior and dependency validation before enabling
    their respective component. This object adds nothing to model requests.
    """

    contract: CvmMode = "off"
    evidence: CvmMode = "off"
    completion: CvmMode = "off"
    signals: CvmMode = "off"
    progress: CvmMode = "off"

    def __post_init__(self) -> None:
        for item in fields(self):
            mode = getattr(self, item.name)
            if mode not in ("off", "shadow", "active"):
                raise ValueError(f"CVM {item.name} must be off, shadow or active")
            if item.name == 'evidence':
                if mode == 'active':
                    raise ValueError('CVM evidence supports off or shadow only in stage 2')
                if mode == 'shadow' and self.contract == 'off':
                    raise ValueError('CVM evidence=shadow requires contract=shadow or active')
            elif item.name != "contract" and mode != "off":
                raise ValueError(f"CVM {item.name}={mode} is not implemented; this component supports off only")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> CvmConfig:
        return cls(**{item.name: environ.get(f"CODEAGENT_CVM_{item.name.upper()}", "off").strip().lower()
                      for item in fields(cls)})

    def snapshot(self) -> dict[str, str | int]:
        """Credential-free configuration metadata; not a CVM runtime checkpoint."""
        return {"schema_version": 1, **asdict(self)}
