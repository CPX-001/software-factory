"""Small deterministic policy over semantic work characteristics and an actual catalog."""

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path

from .skill_catalog import Catalog, skill_identifier
from .workflow import WorkflowError


@dataclass(frozen=True)
class Work:
    domain: str = "general"
    intent: str = "implement"
    risk: str = "normal"
    ui: bool = False
    concerns: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.concerns, (tuple, list, set, frozenset)):
            raise WorkflowError("Work concerns must be a collection of semantic tags, not text")
        for value in (self.domain, self.intent, *self.concerns):
            skill_identifier(value)
        if self.risk not in ("low", "normal", "high", "critical") or type(self.ui) is not bool:
            raise WorkflowError("Work requires risk low/normal/high/critical and a boolean ui")
        object.__setattr__(self, "concerns", tuple(sorted(set(self.concerns))))

    @property
    def tags(self):
        tags = set(self.concerns) | {self.domain, f"intent:{self.intent}", f"risk:{self.risk}"}
        if self.ui or self.domain in ("frontend", "ux"):
            tags.add("ui")
        return tags


@dataclass(frozen=True)
class Rule:
    id: str
    candidates: tuple[str, ...]
    group: str
    reason: str
    all_of: tuple[str, ...] = ()
    any_of: tuple[str, ...] = ()
    unless: tuple[str, ...] = ()
    priority: int = 50
    required: bool = False

    def matches(self, tags):
        return (set(self.all_of) <= tags and (not self.any_of or bool(set(self.any_of) & tags))
                and not set(self.unless) & tags)


def engineering(name):
    return (f"agent-skills:{name}", name)


# These are verified skill names, not an inventory of what is installed. Availability
# always comes from Catalog. Ordered candidates are alternatives, never a bulk activation.
DEFAULT_RULES = (
    Rule("critical-security", engineering("security-and-hardening"), "security",
         "Critical security work requires a hardening review", all_of=("security", "risk:critical"), required=True),
    Rule("security", engineering("security-and-hardening"), "security", "Security-sensitive work",
         all_of=("security",), unless=("risk:critical",), priority=95),
    Rule("architecture", engineering("api-and-interface-design"), "interfaces", "Define system and module boundaries",
         any_of=("architecture", "api"), unless=("intent:diagram",), priority=75),
    Rule("architecture-diagram", ("archify",), "diagram", "Visualize architecture or technical flow",
         all_of=("intent:diagram",), any_of=("architecture", "api", "infrastructure"), priority=90),
    Rule("ui-design", ("frontend-design-premium:frontend-design-premium", "frontend-design-premium",
                       "ui-ux-pro-max", "frontend-design", "design", "ui-styling"), "ui-design",
         "One primary visual and interaction design approach", all_of=("ui",),
         any_of=("intent:design", "intent:implement"), priority=80),
    Rule("visual-review", ("reviewing-design-work", "web-interface-guidelines-review"), "visual-review",
         "Review existing UI instead of loading a creation workflow", all_of=("ui", "intent:review"), priority=80),
    Rule("ux-audit", ("product-design:audit",), "visual-review", "Evidence-based product flow audit",
         all_of=("ui", "intent:audit"), priority=85),
    Rule("design-system", ("design-system",), "design-system", "Shared component and token architecture",
         all_of=("ui", "design-system"), unless=("intent:review", "intent:audit"), priority=65),
    Rule("design-consistency", ("design-system-consistency",), "design-system", "Inspect drift from an existing design system",
         all_of=("ui", "design-system"), any_of=("intent:review", "intent:audit"), priority=65),
    Rule("accessibility", ("frontend-a11y",), "accessibility", "Inspect rendered accessibility",
         all_of=("ui", "accessibility"), priority=90),
    Rule("visual-alternatives", ("product-design:ideate",), "ui-design", "Explore image-based design alternatives",
         all_of=("ui", "intent:explore"), priority=90),
    Rule("image-reference", ("product-design:image-to-code",), "ui-design", "Implement a visual reference faithfully",
         all_of=("ui", "intent:from-image"), priority=90),
    Rule("url-reference", ("product-design:url-to-code",), "ui-design", "Implement a live visual reference",
         all_of=("ui", "intent:from-url"), priority=90),
    Rule("visual-polish", ("visual-polish",), "visual-review", "Inspect visual execution quality",
         all_of=("ui", "intent:polish"), priority=80),
    Rule("debugging", engineering("debugging-and-error-recovery"), "debugging", "Systematic root-cause investigation",
         any_of=("debugging", "intent:debug"), priority=90),
    Rule("testing", engineering("test-driven-development"), "testing", "Behavioral tests and test-driven changes",
         any_of=("testing", "tdd", "intent:test", "intent:tdd"), priority=80),
    Rule("performance", engineering("performance-optimization"), "performance", "Measure and address bottlenecks",
         any_of=("performance", "intent:optimize"), priority=85),
    Rule("simplification", engineering("code-simplification"), "refactor", "Simplify without changing behavior",
         any_of=("simplification", "intent:refactor", "intent:simplify"), priority=75),
    Rule("code-review", engineering("code-review-and-quality"), "code-review", "Review code changes",
         all_of=("intent:review",), unless=("ui",), priority=80),
    Rule("planning", engineering("planning-and-task-breakdown"), "planning", "Progressive roadmap and coherent vertical slices",
         all_of=("intent:plan",), priority=70),
    Rule("specification", engineering("spec-driven-development"), "specification", "Define product requirements and scope",
         any_of=("intent:specify", "specification"), priority=70),
    Rule("git", engineering("git-workflow-and-versioning"), "git", "Git operations explicitly in scope",
         any_of=("git", "intent:git")),
    Rule("ci", engineering("ci-cd-and-automation"), "ci", "Build and deployment automation",
         any_of=("ci", "intent:ci")),
    Rule("adr", engineering("documentation-and-adrs"), "documentation", "Record architecture decisions",
         all_of=("architecture", "intent:document"), priority=80),
    Rule("delegation", ("orchestrating-agent-delegation",), "coordination", "Explicitly requested agent delegation",
         all_of=("delegation",), unless=("agent-qa", "panel")),
    Rule("agent-qa", ("orchestrating-elite-agent-qa",), "coordination", "Explicit multi-agent quality workflow",
         all_of=("agent-qa",), unless=("panel",)),
    Rule("panel", ("complex-enough:orchestrate-multi-perspective-panel",), "coordination", "Explicit independent perspectives",
         all_of=("panel",)),
) + tuple(
    Rule(tag, (name,), group, f"Specialized UI concern: {tag}", all_of=("ui", tag), priority=85)
    for tag, name, group in (
        ("contrast-apca", "apca-contrast", "contrast-apca"),
        ("contrast-wcag", "wcag-contrast", "contrast-wcag"),
        ("color-space", "oklch-color-space", "color-space"),
        ("palette", "palette-relationships", "palette"),
        ("typography", "type-scale", "typography"),
        ("line-height", "line-height-grid", "line-height"),
        ("spacing", "spacing-system", "spacing"),
        ("component-sizing", "component-sizing-principles", "component-sizing"),
        ("tokens-dtcg", "dtcg-format", "token-format"),
        ("token-naming", "token-naming-conventions", "token-naming"),
        ("brand", "brand", "brand"),
        ("banner", "banner-design", "ui-design"),
    )
)


@dataclass(frozen=True)
class Policy:
    max_recommended: int = 3
    disabled_rules: tuple[str, ...] = ()
    bindings: dict[str, tuple[str, ...]] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    recommended: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
            raise WorkflowError("Unknown skill policy fields")
        maximum = data.get("max_recommended", 3)
        if type(maximum) is not int or not 0 <= maximum <= 5:
            raise WorkflowError("max_recommended must be an integer from 0 to 5")
        def names(value):
            if not isinstance(value, list) or len(value) > 50:
                raise WorkflowError("Policy selectors must be a list of at most 50 names")
            return tuple(dict.fromkeys(skill_identifier(v) for v in value))
        rule_ids = {r.id for r in DEFAULT_RULES}
        disabled = names(data.get("disabled_rules", []))
        bindings = data.get("bindings", {})
        if not isinstance(bindings, dict) or (set(bindings) | set(disabled)) - rule_ids:
            raise WorkflowError("Unknown rule in skill policy override")
        mapped = {key: names(value) for key, value in bindings.items()}
        if any(not value for value in mapped.values()):
            raise WorkflowError("Bindings need at least one candidate; use disabled_rules to disable a rule")
        return cls(maximum, disabled, mapped, names(data.get("required", [])),
                   names(data.get("recommended", [])), names(data.get("exclude", [])))


def load_config(project, path=None):
    """Project policy is explicit and separate from discovered skill metadata."""
    project = Path(project).resolve()
    location = Path(path).expanduser().resolve() if path else project / ".factory" / "skills.json"
    if not location.is_file():
        if path:
            raise WorkflowError(f"Skill configuration does not exist: {location}")
        return (), Policy()
    try:
        data = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowError(f"Invalid skill configuration: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {"roots", "policy"}:
        raise WorkflowError("Skill configuration accepts only roots and policy")
    roots = data.get("roots", [])
    if not isinstance(roots, list) or any(not isinstance(root, str) or not root.strip() for root in roots):
        raise WorkflowError("Skill roots must be a list of nonempty paths (relative to the project)")
    return tuple(roots), Policy.from_dict(data.get("policy", {}))


@dataclass
class Routing:
    required: list[str] = field(default_factory=list)
    recommended: list[str] = field(default_factory=list)
    unavailable: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    suppressed: list[dict] = field(default_factory=list)

    def context(self):
        if self.blocked:
            raise WorkflowError("Skill routing is blocked: " + "; ".join(item["rule"] for item in self.blocked))
        return ("Required skills (policy): " + (", ".join(self.required) or "none") + ".\n"
                "Recommended skills: " + (", ".join(self.recommended) or "none") + ".\n"
                "Read required skills before work; consider recommendations when useful. "
                "You may discover and use other available skills when clearly useful. "
                "Selection does not mean a skill has been used.")

    def as_dict(self):
        return {**asdict(self), "status": "blocked" if self.blocked else "ready",
                "context": None if self.blocked else self.context()}

    def required_inputs(self, catalog: Catalog):
        """Official skill input shapes for a future turn; selection never runs a turn.

        Paths matter when discovery used connection-local extra roots. Recommendations
        remain hints; this method injects only policy requirements and their prerequisites.
        """
        self.context()  # fail closed before producing any executable input
        inputs = []
        for name in self.required:
            skill, reason = catalog.resolve(name)
            if reason:
                raise WorkflowError(f"Required skill {name} is no longer available: {reason}")
            inputs.append({"type": "skill", "name": skill.name, "path": skill.path})
        return inputs


class SkillRouter:
    def __init__(self, catalog: Catalog, policy=None):
        self.catalog = catalog
        self.policy = policy or Policy()
        # Validate programmatic policies just as strictly as JSON configuration.
        self.policy = Policy.from_dict(json.loads(json.dumps(asdict(self.policy))))

    def route(self, work: Work):
        result = Routing()
        rules = [r for r in DEFAULT_RULES if r.id not in self.policy.disabled_rules and r.matches(work.tags)]
        groups = {candidate: r.group for r in DEFAULT_RULES for candidate in r.candidates}
        for level in ("required", "recommended"):
            for index, selector in enumerate(getattr(self.policy, level)):
                rules.append(Rule(f"project-{level}-{index}", (selector,), groups.get(selector, selector),
                                  "Explicit project policy", priority=100, required=level == "required"))
        rules.sort(key=lambda r: (not r.required, -r.priority, r.id))
        selected, selected_groups = set(), set()
        for rule in rules:
            if not rule.required and rule.group in selected_groups:
                result.suppressed.append({"rule": rule.id, "reason": "equivalent capability already selected"})
                continue
            level = "required" if rule.required else "recommended"
            candidates = self.policy.bindings.get(rule.id, rule.candidates)
            bundle = None
            for candidate in candidates:
                skill, reason = self._resolve(candidate)
                if reason:
                    result.unavailable.append({"skill": candidate, "level": level, "rule": rule.id, "reason": reason})
                    continue
                bundle = [skill.id]
                # Verified composition requirement from frontend-design-premium/SKILL.md.
                # A recommendation with a missing prerequisite is unusable; try an alternative.
                if skill.id.rsplit(":", 1)[-1] == "frontend-design-premium":
                    prefix = skill.id.rsplit(":", 1)[0] + ":" if ":" in skill.id else ""
                    dependency, reason = self._resolve(prefix + "frontend-design")
                    if reason:
                        result.unavailable.append({"skill": prefix + "frontend-design", "level": level,
                                                   "rule": rule.id, "reason": f"prerequisite: {reason}"})
                        bundle = None
                        continue
                    bundle.append(dependency.id)
                if not rule.required and len(result.recommended) + len(set(bundle) - selected) > self.policy.max_recommended:
                    result.suppressed.append({"rule": rule.id, "skill": candidate, "reason": "recommendation budget"})
                    bundle = None
                    continue
                break
            if bundle is None:
                if rule.required:
                    result.blocked.append({"rule": rule.id, "candidates": list(candidates), "reason": "required skill unavailable"})
                continue
            additions = [name for name in bundle if name not in selected]
            getattr(result, level).extend(additions)
            for name in additions:
                result.reasons[name] = rule.reason if name == bundle[0] else f"Prerequisite of {bundle[0]}"
            selected.update(bundle)
            selected_groups.add(rule.group)
        # One attempted alias should not create duplicate missing reports.
        result.unavailable = list({(m["skill"], m["level"], m["rule"]): m for m in result.unavailable}.values())
        return result

    def _resolve(self, selector):
        skill, reason = self.catalog.resolve(selector)
        excluded = set(self.policy.exclude)
        if selector in excluded or (skill and {skill.id, skill.name, skill.id.rsplit(":", 1)[-1]} & excluded):
            return None, "excluded_by_project"
        return skill, reason
