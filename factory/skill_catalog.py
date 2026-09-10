"""Read Codex's own catalog; never equate a cached plugin with an enabled skill."""

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import subprocess
import sys

from .workflow import WorkflowError

IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,199}$")


def skill_identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise WorkflowError(f"Invalid skill identifier: {value!r}")
    return value


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    path: str
    description: str = ""
    enabled: bool = True
    scope: str = "user"
    source: str = "runtime"


@dataclass(frozen=True)
class Catalog:
    skills: tuple[Skill, ...] = ()
    errors: tuple[str, ...] = ()
    explicit_roots: tuple[str, ...] = ()

    def resolve(self, selector):
        matches = [s for s in self.skills if s.id == selector]
        if not matches and ":" not in selector:
            matches = [s for s in self.skills if s.name == selector or s.id.rsplit(":", 1)[-1] == selector]
        active = [s for s in matches if s.enabled]
        if len(active) == 1:
            return active[0], None
        return None, "ambiguous" if len(active) > 1 else "disabled" if matches else "not_discovered"

    def as_dict(self):
        return asdict(self)


def qualified_id(name, path):
    """Read a real plugin manifest for namespace identity, not for enablement."""
    if ":" in name:
        return skill_identifier(name)
    for parent in path.parents:
        manifest = parent / ".codex-plugin" / "plugin.json"
        if manifest.is_file():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            return skill_identifier(f"{skill_identifier(data['name'])}:{name}")
    return skill_identifier(name)


def catalog_from_response(response, project, roots=()):
    """Strictly scope a runtime response to one cwd; keep disabled entries observable."""
    project = Path(project).resolve()
    configured = tuple(str(Path(root).resolve()) for root in roots)
    entries, errors = {}, []
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise WorkflowError("Invalid skills/list response")
    matched = False
    for entry in response["data"]:
        if not isinstance(entry, dict) or Path(entry.get("cwd", "")).resolve() != project:
            continue
        matched = True
        errors.extend(f"Codex: {error}" for error in entry.get("errors", []))
        for raw in entry.get("skills", []):
            try:
                name = skill_identifier(raw["name"])
                path = Path(raw["path"])
                if not path.is_absolute():
                    raise ValueError("skill path must be absolute")
                path = path.resolve()
                if type(raw["enabled"]) is not bool:
                    raise ValueError("enabled must be boolean")
                source = "explicit_root" if any(path.is_relative_to(Path(root)) for root in configured) else "runtime"
                skill = Skill(qualified_id(name, path), name, str(path), str(raw.get("description", "")),
                              raw["enabled"], str(raw.get("scope", "user")), source)
                key = (skill.id, skill.path)
                # Duplicate metadata must never re-enable an explicitly disabled entry.
                if key not in entries or not skill.enabled:
                    entries[key] = skill
            except (KeyError, TypeError, ValueError, OSError) as exc:
                errors.append(f"Invalid skill metadata: {exc}")
    if not matched:
        errors.append("skills/list did not return the requested project")
    return Catalog(tuple(sorted(entries.values(), key=lambda s: (s.id, s.path))), tuple(errors), configured)


def _runtime_query(project, roots):
    """Run only metadata RPCs. No thread, turn, account login, or LLM request."""
    from openai_codex.client import CodexClient, CodexConfig
    from openai_codex.generated.v2_all import SkillsExtraRootsSetResponse, SkillsListResponse

    client = CodexClient(CodexConfig(cwd=project))
    try:
        client.start()
        client.initialize()
        if roots:
            # Connection-local registration; does not install skills or edit config.toml.
            client.request("skills/extraRoots/set", {"extraRoots": roots}, response_model=SkillsExtraRootsSetResponse)
        result = client.request("skills/list", {"cwds": [project], "forceReload": True},
                                response_model=SkillsListResponse)
        return result.model_dump(mode="json", by_alias=True)
    finally:
        client.close()


def discover_catalog(project, roots=(), *, query=None):
    project = Path(project).resolve()
    if not project.is_dir():
        raise WorkflowError("Project must be an existing directory")
    valid_roots, errors = [], []
    for root in roots:
        path = Path(root).expanduser()
        path = (project / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_dir():
            errors.append(f"Explicit skill root does not exist: {path}")
        elif str(path) not in valid_roots:
            valid_roots.append(str(path))
    try:
        if query is not None:
            response = query(str(project), valid_roots)
        else:
            # Bound metadata startup/scans; isolate SDK imports from stdlib-only CLI/tests.
            process = subprocess.run([sys.executable, "-m", "factory.skill_catalog"],
                                     input=json.dumps({"project": str(project), "roots": valid_roots}),
                                     capture_output=True, text=True, timeout=45)
            if process.returncode:
                raise WorkflowError(process.stderr.strip() or "Codex skills/list failed")
            response = json.loads(process.stdout)
        catalog = catalog_from_response(response, project, valid_roots)
        return Catalog(catalog.skills, tuple(errors) + catalog.errors, catalog.explicit_roots)
    except (WorkflowError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return Catalog(errors=tuple(errors) + (f"Catalog discovery failed: {exc}",), explicit_roots=tuple(valid_roots))


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        print(json.dumps(_runtime_query(request["project"], request["roots"])))
    except ImportError:
        sys.exit("Official Codex SDK unavailable; run with .venv/bin/python")
    except Exception as exc:
        sys.exit(f"{type(exc).__name__}: {exc}")
