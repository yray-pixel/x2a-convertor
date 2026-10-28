"""Data models for AAP Config-as-Code generation."""

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AAPRequestModel(BaseModel):
    """Strict base model for the generator request contract."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AAPConfigFile(AAPRequestModel):
    """Resolved location and top-level variable for one AAP configuration file."""

    path: str = Field(min_length=1)
    variable: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("must be a relative path without parent traversal")
        return value


class AAPProjectRequest(AAPRequestModel):
    """Inputs used to define one AAP Project."""

    organization: str = Field(min_length=1)
    repository_url: str = Field(min_length=1)
    repository_branch: str = Field(min_length=1)
    scm_credential: str | None = Field(default=None, min_length=1)


class AAPJobTemplateRequest(AAPRequestModel):
    """Inputs used to define one AAP Job Template."""

    module_name: str = Field(min_length=1)
    playbook: str = Field(min_length=1)
    inventory: str = Field(min_length=1)
    credentials: list[str] = Field(default_factory=list)
    execution_environment: str | None = Field(default=None, min_length=1)

    @field_validator("playbook")
    @classmethod
    def validate_playbook_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("must be a relative path without parent traversal")
        if path.suffix not in {".yml", ".yaml"}:
            raise ValueError("must identify a YAML playbook")
        return value

    @field_validator("credentials")
    @classmethod
    def validate_credentials(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("must not contain empty credential names")
        if len(normalized) != len(set(normalized)):
            raise ValueError("must not contain duplicate credential names")
        return normalized


class AAPNameComponent:
    """Normalize one source value for use inside a generated AAP name."""

    @classmethod
    def from_raw(cls, value: str) -> str:
        """Return a deterministic lowercase ASCII component."""
        normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
        if not normalized:
            raise ValueError(f"Cannot derive an AAP object name from {value!r}")
        return normalized


class AAPConfigRequest(AAPRequestModel):
    """Complete non-secret input contract for one generator invocation."""

    schema_version: Literal[1] = 1
    repository_path: Path
    configuration_directory: str = Field(min_length=1)
    project: AAPProjectRequest
    job_template: AAPJobTemplateRequest

    @field_validator("configuration_directory")
    @classmethod
    def validate_configuration_directory(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("must be a relative path without parent traversal")
        return value

    @classmethod
    def from_path(cls, request_path: str | Path) -> "AAPConfigRequest":
        """Load and validate a JSON request document."""
        path = Path(request_path)
        if not path.is_file():
            raise ValueError(f"AAP config request does not exist: {path}")
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    @property
    def project_name(self) -> str:
        """Return the generated AAP Project name for the module."""
        return f"x2a_{self._module_name}"

    @property
    def job_template_name(self) -> str:
        """Return the generated AAP Job Template name for the module."""
        return self.project_name

    @property
    def _module_name(self) -> str:
        return AAPNameComponent.from_raw(self.job_template.module_name)


class AAPEntryModel(BaseModel):
    """Base model for fields owned by X2A in an AAP entry."""

    model_config = ConfigDict(frozen=True)
    name: str

    def owned_mapping(self) -> dict[str, Any]:
        """Return configured fields while omitting absent optional fields."""
        return self.model_dump(exclude_none=True)

    @classmethod
    def owned_fields(cls) -> set[str]:
        """Return every field that X2A may update or remove."""
        return set(cls.model_fields)


class AAPProjectEntry(AAPEntryModel):
    """Fields owned by X2A for one AAP Project entry."""

    organization: str
    scm_type: Literal["git"] = "git"
    scm_url: str
    scm_branch: str
    scm_credential: str | None = None
    scm_update_on_launch: bool = True
    scm_clean: bool = True
    scm_delete_on_update: bool = True
    update_project: bool = True
    wait: bool = True

    @classmethod
    def from_request(cls, request: AAPConfigRequest) -> "AAPProjectEntry":
        """Build the project entry from the complete request."""
        return cls(
            name=request.project_name,
            organization=request.project.organization,
            scm_url=request.project.repository_url,
            scm_branch=request.project.repository_branch,
            scm_credential=request.project.scm_credential,
        )


class AAPJobTemplateEntry(AAPEntryModel):
    """Fields owned by X2A for one AAP Job Template entry."""

    organization: str
    project: str
    job_type: Literal["run"] = "run"
    playbook: str
    inventory: str
    credentials: list[str] = Field(default_factory=list)
    execution_environment: str | None = None
    prevent_instance_group_fallback: bool = False

    @classmethod
    def from_request(cls, request: AAPConfigRequest) -> "AAPJobTemplateEntry":
        """Build the job-template entry from the complete request."""
        template = request.job_template
        return cls(
            name=request.job_template_name,
            organization=request.project.organization,
            project=request.project_name,
            playbook=template.playbook,
            inventory=template.inventory,
            credentials=template.credentials,
            execution_environment=template.execution_environment,
        )


class AAPConfigFileResult(BaseModel):
    """Write result for one configuration file."""

    path: Path
    status: Literal["created", "updated", "unchanged"]


class AAPConfigGenerationResult(BaseModel):
    """Structured result returned to the RHDH integration."""

    schema_version: Literal[1] = 1
    changed: bool
    project_name: str
    job_template_name: str
    project_action: Literal["added", "updated", "unchanged"]
    job_template_action: Literal["added", "updated", "unchanged"]
    files: list[AAPConfigFileResult]

    @classmethod
    def from_generation(
        cls,
        request: AAPConfigRequest,
        project_action: Literal["added", "updated", "unchanged"],
        template_action: Literal["added", "updated", "unchanged"],
        files: list[AAPConfigFileResult],
    ) -> "AAPConfigGenerationResult":
        """Build a result from entry actions and file results."""
        return cls(
            changed=any(item.status != "unchanged" for item in files),
            project_name=request.project_name,
            job_template_name=request.job_template_name,
            project_action=project_action,
            job_template_action=template_action,
            files=files,
        )
