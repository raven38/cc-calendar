"""Conservative canonical project assignment."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import PurePath

from .models import CanonicalProject, ProjectAssignment, Session


class ProjectAssigner:
    """Resolve a session to a canonical project without changing provider data."""

    def __init__(
        self,
        projects: dict[str, CanonicalProject],
        *,
        native_mappings: Mapping[tuple[str, str, str, str], str] | None = None,
        repo_mappings: Mapping[str, str] | None = None,
        path_mappings: Mapping[str, str] | None = None,
        provider_name_mappings: Mapping[tuple[str, str, str, str], str] | None = None,
        classifier: Callable[[Session], tuple[str, float] | None] | None = None,
    ) -> None:
        self.projects = projects
        self.native_mappings = dict(native_mappings or {})
        self.repo_mappings = dict(repo_mappings or {})
        self.path_mappings = dict(path_mappings or {})
        self.provider_name_mappings = dict(provider_name_mappings or {})
        self.classifier = classifier

    def assign(self, session: Session) -> ProjectAssignment:
        native_id = _native_project_id(session.external_project_ref)
        native_key = (session.provider, session.product, session.source_instance, native_id)
        if native_id and native_key in self.native_mappings:
            assignment = self._mapped(self.native_mappings[native_key], "native", 1.0)
            if assignment is not None:
                return assignment

        if session.repo and session.repo in self.repo_mappings:
            assignment = self._mapped(self.repo_mappings[session.repo], "repo", 0.95)
            if assignment is not None:
                return assignment

        if session.cwd:
            path = PurePath(session.cwd)
            for root, project_id in self.path_mappings.items():
                if path == PurePath(root) or PurePath(root) in path.parents:
                    assignment = self._mapped(project_id, "path", 0.9)
                    if assignment is not None:
                        return assignment

        native_name = _native_project_name(session.external_project_ref)
        name_key = (session.provider, session.product, session.source_instance, native_name)
        if native_name and name_key in self.provider_name_mappings:
            assignment = self._mapped(self.provider_name_mappings[name_key], "provider_name", 0.8)
            if assignment is not None:
                return assignment

        if self.classifier is not None:
            suggestion = self.classifier(session)
            if suggestion is not None:
                project_id, confidence = suggestion
                if project_id in self.projects:
                    return ProjectAssignment(project_id, "classifier", confidence)

        return ProjectAssignment("__misc__", "misc", 1.0)

    def _mapped(
        self, project_id: str, provenance: str, confidence: float
    ) -> ProjectAssignment | None:
        if project_id not in self.projects:
            return None
        return ProjectAssignment(project_id, provenance, confidence)


def _native_project_id(ref: object) -> str | None:
    if isinstance(ref, Mapping):
        value = ref.get("native_project_id")
        return value if isinstance(value, str) and value else None
    value = getattr(ref, "native_project_id", None)
    return value if isinstance(value, str) and value else None


def _native_project_name(ref: object) -> str | None:
    if isinstance(ref, Mapping):
        value = ref.get("native_project_name")
        return value if isinstance(value, str) and value else None
    value = getattr(ref, "native_project_name", None)
    return value if isinstance(value, str) and value else None
