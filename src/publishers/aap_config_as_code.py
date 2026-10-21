"""Generate AAP Config-as-Code entries from a versioned request."""

import re
import tempfile
from collections.abc import Mapping
from io import StringIO
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq


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

    def resolve_file_requests(
        self, repository_path: Path, yaml: YAML
    ) -> tuple[AAPConfigFile, AAPConfigFile]:
        """Discover the fixed configuration files and their top-level keys."""
        directory = Path(self.configuration_directory)
        return (
            self._discover_file(
                repository_path,
                directory / "controller_projects.yml",
                "controller_projects",
                yaml,
            ),
            self._discover_file(
                repository_path,
                directory / "controller_job_templates.yml",
                "controller_templates",
                yaml,
            ),
        )

    @staticmethod
    def _discover_file(
        repository_path: Path, relative_path: Path, key_prefix: str, yaml: YAML
    ) -> AAPConfigFile:
        path = (repository_path / relative_path).resolve()
        if not path.is_relative_to(repository_path):
            raise ValueError(
                f"Config file path escapes the repository: {relative_path}"
            )
        if not path.is_file():
            raise ValueError(
                f"Required AAP config file does not exist: {relative_path}"
            )
        with path.open(encoding="utf-8") as stream:
            document = yaml.load(stream)
        if not isinstance(document, Mapping):
            raise TypeError(f"{relative_path} must contain a YAML mapping")
        matches = [
            key
            for key in document
            if isinstance(key, str) and key.startswith(key_prefix)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"{relative_path} must contain exactly one top-level key beginning with "
                f"{key_prefix!r}"
            )
        if not isinstance(document[matches[0]], list):
            raise TypeError(f"{relative_path} field {matches[0]} must be a YAML list")
        return AAPConfigFile(path=relative_path.as_posix(), variable=matches[0])


class AAPNameComponent:
    """Normalize one source value for use inside a generated AAP name."""

    @classmethod
    def from_raw(cls, value: str) -> str:
        """Return a deterministic lowercase ASCII component."""
        normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
        if not normalized:
            raise ValueError(f"Cannot derive an AAP object name from {value!r}")
        return normalized


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


class AAPConfigDocument:
    """One loaded round-trip YAML document and its target collection."""

    def __init__(self, path: Path, variable: str, yaml: YAML) -> None:
        self.path = path
        self.variable = variable
        self.yaml = yaml
        self.existed = path.exists()
        self.document = self._load()
        self.original = self._serialize()
        self.entries = self._entries()

    def upsert(self, entry: AAPEntryModel) -> Literal["added", "updated", "unchanged"]:
        """Add or update one entry while preserving unrelated fields."""
        desired = entry.owned_mapping()
        for current in self.entries:
            if isinstance(current, dict) and current.get("name") == entry.name:
                return self._update(current, desired, entry.owned_fields())
        self.entries.append(CommentedMap(desired))
        return "added"

    def render(self) -> str:
        """Serialize the possibly updated document."""
        return self._serialize()

    def _load(self) -> CommentedMap:
        if not self.existed:
            return CommentedMap()
        with self.path.open(encoding="utf-8") as stream:
            document = self.yaml.load(stream)
        if document is None:
            return CommentedMap()
        if not isinstance(document, CommentedMap):
            raise TypeError(f"{self.path} must contain a YAML mapping")
        return document

    def _entries(self) -> CommentedSeq:
        values = self.document.setdefault(self.variable, CommentedSeq())
        if not isinstance(values, CommentedSeq):
            raise TypeError(f"{self.path} field {self.variable} must be a YAML list")
        self._validate_entries(values)
        return values

    def _validate_entries(self, values: CommentedSeq) -> None:
        names: set[str] = set()
        for index, entry in enumerate(values):
            if not isinstance(entry, CommentedMap) or not isinstance(
                entry.get("name"), str
            ):
                raise TypeError(
                    f"{self.path} field {self.variable}[{index}] must be a mapping with a name"
                )
            if entry["name"] in names:
                raise ValueError(
                    f"{self.path} field {self.variable} contains duplicate name {entry['name']!r}"
                )
            names.add(entry["name"])

    def _serialize(self) -> str:
        stream = StringIO()
        self.yaml.dump(self.document, stream)
        return stream.getvalue()

    @staticmethod
    def _update(
        current: dict[str, Any], desired: dict[str, Any], owned_fields: set[str]
    ) -> Literal["updated", "unchanged"]:
        before = {
            field: current.get(field) for field in owned_fields if field in current
        }
        for field in owned_fields - desired.keys():
            current.pop(field, None)
        for field, value in desired.items():
            current[field] = value
        after = {
            field: current.get(field) for field in owned_fields if field in current
        }
        return "unchanged" if before == after else "updated"


class AAPConfigAsCodeGenerator:
    """Merge one project and job template into a checked-out config repository."""

    def __init__(self, request: AAPConfigRequest) -> None:
        self.request = request
        self.repository_path = request.repository_path.resolve()
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        self.yaml.width = 120
        self.yaml.explicit_start = True
        self.yaml.indent(mapping=2, sequence=4, offset=2)

    def generate(self) -> AAPConfigGenerationResult:
        """Validate both documents, merge entries, then write changed files."""
        self._validate_repository()
        projects_file, job_templates_file = self.request.resolve_file_requests(
            self.repository_path, self.yaml
        )
        project_doc = self._document(projects_file)
        template_doc = self._document(job_templates_file)
        if project_doc.path == template_doc.path:
            raise ValueError("Projects and job templates files must be different")
        project_action = project_doc.upsert(AAPProjectEntry.from_request(self.request))
        template_action = template_doc.upsert(
            AAPJobTemplateEntry.from_request(self.request)
        )
        files = [self._persist(project_doc), self._persist(template_doc)]
        return AAPConfigGenerationResult.from_generation(
            self.request, project_action, template_action, files
        )

    def _validate_repository(self) -> None:
        if not self.repository_path.is_dir():
            raise ValueError(
                f"AAP config repository does not exist: {self.repository_path}"
            )

    def _document(self, config: AAPConfigFile) -> AAPConfigDocument:
        path = (self.repository_path / config.path).resolve()
        if not path.is_relative_to(self.repository_path):
            raise ValueError(f"Config file path escapes the repository: {config.path}")
        return AAPConfigDocument(path, config.variable, self.yaml)

    def _persist(self, document: AAPConfigDocument) -> AAPConfigFileResult:
        rendered = document.render()
        if rendered == document.original:
            return AAPConfigFileResult(path=document.path, status="unchanged")
        status: Literal["created", "updated"] = (
            "updated" if document.existed else "created"
        )
        self._write(document.path, rendered)
        return AAPConfigFileResult(path=document.path, status=status)

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing_mode = path.stat().st_mode if path.exists() else None
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(content)
        if existing_mode is not None:
            temporary_path.chmod(existing_mode)
        temporary_path.replace(path)
