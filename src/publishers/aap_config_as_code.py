"""Generate AAP Config-as-Code entries from a versioned request."""

import tempfile
from collections.abc import Mapping
from io import StringIO
from pathlib import Path
from typing import Any, Literal

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from src.publishers.models import (
    AAPConfigFile,
    AAPConfigFileResult,
    AAPConfigGenerationResult,
    AAPConfigRequest,
    AAPEntryModel,
    AAPJobTemplateEntry,
    AAPProjectEntry,
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
        projects_file, job_templates_file = self._resolve_file_requests()
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

    def _resolve_file_requests(self) -> tuple[AAPConfigFile, AAPConfigFile]:
        """Resolve configuration files and their top-level keys."""
        directory = Path(self.request.configuration_directory)
        return (
            self._discover_file(
                directory / "controller_projects.yml", "controller_projects"
            ),
            self._discover_file(
                directory / "controller_job_templates.yml", "controller_templates"
            ),
        )

    def _discover_file(self, relative_path: Path, key_prefix: str) -> AAPConfigFile:
        path = (self.repository_path / relative_path).resolve()
        if not path.is_relative_to(self.repository_path):
            raise ValueError(
                f"Config file path escapes the repository: {relative_path}"
            )
        if not path.is_file():
            return AAPConfigFile(
                path=relative_path.as_posix(), variable=f"{key_prefix}_x2a"
            )
        with path.open(encoding="utf-8") as stream:
            document = self.yaml.load(stream)
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
