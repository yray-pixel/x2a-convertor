"""Tests for versioned AAP Config-as-Code generation."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from src.publishers.aap_config_as_code import (
    AAPConfigAsCodeGenerator,
    AAPConfigRequest,
)


class AAPRequestFactory:
    """Build valid generator requests with focused test overrides."""

    @classmethod
    def payload(cls, repository_path: Path) -> dict:
        return {
            "schema_version": 1,
            "repository_path": str(repository_path),
            "configuration_directory": "config/test",
            "project": {
                "organization": "ray-test",
                "repository_url": "https://github.com/acme/migrated.git",
                "repository_branch": "main",
                "scm_credential": None,
            },
            "job_template": {
                "module_name": "Nginx Multisite",
                "playbook": "project-123/ansible/run_nginx_multisite.yml",
                "inventory": "ray-test",
                "credentials": [],
                "execution_environment": None,
            },
        }

    @classmethod
    def request(cls, repository_path: Path) -> AAPConfigRequest:
        if repository_path.is_dir():
            directory = repository_path / "config/test"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "controller_projects.yml").write_text(
                "controller_projects_x2a_test: []\n"
            )
            (directory / "controller_job_templates.yml").write_text(
                "controller_templates_x2a_test: []\n"
            )
        return AAPConfigRequest.model_validate(cls.payload(repository_path))


class TestAAPConfigRequest:
    def test_loads_versioned_request(self, tmp_path):
        path = tmp_path / "request.json"
        path.write_text(json.dumps(AAPRequestFactory.payload(tmp_path)))

        request = AAPConfigRequest.from_path(path)

        assert request.project_name == "x2a_nginx_multisite"
        assert request.job_template_name == "x2a_nginx_multisite"

    def test_rejects_unknown_fields_that_could_contain_secrets(self, tmp_path):
        payload = AAPRequestFactory.payload(tmp_path)
        payload["config_repository_token"] = "secret"

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            AAPConfigRequest.model_validate(payload)

    def test_rejects_duplicate_credentials(self, tmp_path):
        payload = AAPRequestFactory.payload(tmp_path)
        payload["job_template"]["credentials"] = ["Machine", "Machine"]

        with pytest.raises(ValidationError, match="duplicate credential"):
            AAPConfigRequest.model_validate(payload)

    def test_rejects_directory_traversal(self, tmp_path):
        payload = AAPRequestFactory.payload(tmp_path)
        payload["configuration_directory"] = "../outside"

        with pytest.raises(ValidationError, match="parent traversal"):
            AAPConfigRequest.model_validate(payload)


class TestAAPConfigAsCodeGenerator:
    def test_discovers_directory_files_and_top_level_keys(self, tmp_path):
        payload = AAPRequestFactory.payload(tmp_path)
        request = AAPConfigRequest.model_validate(payload)
        directory = tmp_path / "config/test"
        directory.mkdir(parents=True)
        (directory / "controller_projects.yml").write_text(
            "controller_projects_customer:\n  - name: Existing\n"
        )
        (directory / "controller_job_templates.yml").write_text(
            "controller_templates_customer: []\n"
        )

        result = AAPConfigAsCodeGenerator(request).generate()

        assert result.changed is True
        assert (
            self._load(directory / "controller_projects.yml")[
                "controller_projects_customer"
            ][1]["name"]
            == "x2a_nginx_multisite"
        )
        assert (
            self._load(directory / "controller_job_templates.yml")[
                "controller_templates_customer"
            ][0]["name"]
            == "x2a_nginx_multisite"
        )

    @pytest.mark.parametrize(
        ("filename", "content", "message"),
        [
            ("controller_projects.yml", "{}\n", "exactly one"),
            (
                "controller_projects.yml",
                "controller_projects_a: []\ncontroller_projects_b: []\n",
                "exactly one",
            ),
            ("controller_projects.yml", "controller_projects_a: {}\n", "YAML list"),
        ],
    )
    def test_rejects_invalid_directory_key_discovery(
        self, tmp_path, filename, content, message
    ):
        payload = AAPRequestFactory.payload(tmp_path)
        request = AAPConfigRequest.model_validate(payload)
        directory = tmp_path / "config/test"
        directory.mkdir(parents=True)
        (directory / filename).write_text(content)
        if filename == "controller_projects.yml":
            (directory / "controller_job_templates.yml").write_text(
                "controller_templates_a: []\n"
            )

        with pytest.raises((TypeError, ValueError), match=message):
            AAPConfigAsCodeGenerator(request).generate()

    def test_rejects_missing_directory_file_before_writing(self, tmp_path):
        payload = AAPRequestFactory.payload(tmp_path)
        request = AAPConfigRequest.model_validate(payload)
        directory = tmp_path / "config/test"
        directory.mkdir(parents=True)
        projects = directory / "controller_projects.yml"
        projects.write_text("controller_projects_a: []\n")

        with pytest.raises(ValueError, match="does not exist"):
            AAPConfigAsCodeGenerator(request).generate()

        assert projects.read_text() == "controller_projects_a: []\n"

    def test_rejects_directory_traversal(self, tmp_path):
        payload = AAPRequestFactory.payload(tmp_path)
        payload["configuration_directory"] = "../outside"

        with pytest.raises(ValidationError, match="parent traversal"):
            AAPConfigRequest.model_validate(payload)

    def test_generates_schema_correct_entries(self, tmp_path):
        request = AAPRequestFactory.request(tmp_path)
        request.project.scm_credential = "GitHub Source"
        request.job_template.credentials = ["Linux Machine", "Vault"]
        request.job_template.execution_environment = "Default execution environment"

        result = AAPConfigAsCodeGenerator(request).generate()
        projects = self._load(result.files[0].path)
        templates = self._load(result.files[1].path)

        project = projects["controller_projects_x2a_test"][0]
        assert project["name"] == "x2a_nginx_multisite"
        assert project["scm_credential"] == "GitHub Source"
        assert project["update_project"] is True
        template = templates["controller_templates_x2a_test"][0]
        assert template["name"] == "x2a_nginx_multisite"
        assert template["playbook"] == request.job_template.playbook
        assert template["credentials"] == ["Linux Machine", "Vault"]
        assert template["execution_environment"] == "Default execution environment"
        assert template["prevent_instance_group_fallback"] is False
        assert result.changed is True

    def test_preserves_unrelated_entries_fields_and_comments(self, tmp_path):
        request = AAPRequestFactory.request(tmp_path)
        projects = tmp_path / "config/test/controller_projects.yml"
        projects.write_text(
            "# keep document comment\n"
            "controller_projects_x2a_test:\n"
            "  - name: Existing\n"
            "    organization: Other\n"
            "  - name: x2a_nginx_multisite\n"
            "    # keep entry comment\n"
            "    organization: Old\n"
            "    scm_url: https://old.example/repo.git\n"
            "    labels:\n"
            "      - custom\n"
        )

        result = AAPConfigAsCodeGenerator(request).generate()
        content = projects.read_text()
        document = self._load(projects)

        assert "# keep document comment" in content
        assert "# keep entry comment" in content
        assert "controller_projects_x2a_test:\n  - name: Existing\n" in content
        assert document["controller_projects_x2a_test"][0]["name"] == "Existing"
        generated = document["controller_projects_x2a_test"][1]
        assert generated["labels"] == ["custom"]
        assert generated["scm_url"] == request.project.repository_url
        assert result.project_action == "updated"

    def test_removes_stale_optional_owned_fields(self, tmp_path):
        request = AAPRequestFactory.request(tmp_path)
        AAPConfigAsCodeGenerator(request).generate()
        request.project.scm_credential = "Temporary SCM"
        request.job_template.execution_environment = "Temporary EE"
        AAPConfigAsCodeGenerator(request).generate()

        request.project.scm_credential = None
        request.job_template.execution_environment = None
        AAPConfigAsCodeGenerator(request).generate()
        project = self._load(tmp_path / "config/test/controller_projects.yml")[
            "controller_projects_x2a_test"
        ][0]
        template = self._load(tmp_path / "config/test/controller_job_templates.yml")[
            "controller_templates_x2a_test"
        ][0]

        assert "scm_credential" not in project
        assert "execution_environment" not in template

    def test_second_generation_is_idempotent(self, tmp_path):
        request = AAPRequestFactory.request(tmp_path)
        first = AAPConfigAsCodeGenerator(request).generate()
        content = [item.path.read_text() for item in first.files]

        second = AAPConfigAsCodeGenerator(request).generate()

        assert second.changed is False
        assert second.project_action == "unchanged"
        assert second.job_template_action == "unchanged"
        assert [item.status for item in second.files] == ["unchanged", "unchanged"]
        assert [item.path.read_text() for item in second.files] == content

    def test_validates_both_files_before_writing(self, tmp_path):
        request = AAPRequestFactory.request(tmp_path)
        projects = tmp_path / "config/test/controller_projects.yml"
        templates = tmp_path / "config/test/controller_job_templates.yml"
        projects.write_text("# unchanged\ncontroller_projects_x2a_test: []\n")
        templates.write_text("controller_templates_x2a_test: {}\n")

        with pytest.raises(TypeError, match="must be a YAML list"):
            AAPConfigAsCodeGenerator(request).generate()

        assert projects.read_text() == "# unchanged\ncontroller_projects_x2a_test: []\n"

    def test_rejects_symlink_escape(self, tmp_path):
        request = AAPRequestFactory.request(tmp_path)
        outside = tmp_path.parent / f"{tmp_path.name}-outside"
        outside.mkdir()
        for path in (tmp_path / "config/test").iterdir():
            path.unlink()
        (tmp_path / "config/test").rmdir()
        (tmp_path / "config").rmdir()
        (tmp_path / "config").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="escapes the repository"):
            AAPConfigAsCodeGenerator(request).generate()

    def test_rejects_missing_repository(self, tmp_path):
        request = AAPRequestFactory.request(tmp_path / "missing")

        with pytest.raises(ValueError, match="repository does not exist"):
            AAPConfigAsCodeGenerator(request).generate()

    @staticmethod
    def _load(path: Path) -> dict:
        return YAML(typ="safe").load(path.read_text())
