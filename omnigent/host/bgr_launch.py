"""Host-local fixed-profile records; no runner launch wire or owner credentials."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from omnigent.bgr_records import Identifier, Record
from omnigent.bgr_supervision import Sha256, SupervisedGeneration


class AdminStubProfile(Record):
    """Administrator-provisioned harmless image, never a native runtime profile."""

    kind: Literal["harmless-stub-v1"] = "harmless-stub-v1"
    profile_id: Identifier
    root_directory: str
    worker_uid: Annotated[int, Field(ge=1000, le=2147483647)]
    worker_gid: Annotated[int, Field(ge=1000, le=2147483647)]
    host_uid: Annotated[int, Field(ge=1)]
    gate: Literal["/usr/libexec/omnigent-bgr-gate"] = "/usr/libexec/omnigent-bgr-gate"
    workload: Literal["/usr/libexec/omnigent-bgr-stub"] = "/usr/libexec/omnigent-bgr-stub"

    @model_validator(mode="after")
    def separate_identity(self) -> Self:
        if self.worker_uid == self.host_uid:
            raise ValueError("Worker must not use the host daemon identity")
        return self


class StubStartRequest(Record):
    generation: SupervisedGeneration
    profile_id: Identifier
    manifest_id: Identifier
    domain_id: Identifier
    root_claim_id: Identifier


class UnitLaunch(Record):
    generation_id: Identifier
    launch_id: Identifier
    supervisor_incarnation: Identifier
    invocation_id: Identifier
    barrier_id: Identifier
    profile_sha256: Sha256
    domain_sha256: Sha256

    @property
    def slice_unit(self) -> str:
        return f"bgr{self.generation_id}.slice"

    @property
    def service_unit(self) -> str:
        return f"bgr{self.generation_id}.service"


class StubLaunchPlan(Record):
    request: StubStartRequest
    profile_sha256: Sha256
    supervisor_incarnation: Identifier
    barrier_id: Identifier

    @property
    def slice_unit(self) -> str:
        return f"bgr{self.request.generation.generation_id}.slice"

    @property
    def service_unit(self) -> str:
        return f"bgr{self.request.generation.generation_id}.service"
