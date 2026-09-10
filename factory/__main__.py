"""Local factory CLI, including resumable conversational discovery."""

import argparse
import json
import sqlite3

from .workflow import Store, WorkflowError, next_action


def show_discovery(snapshot):
    discovery = snapshot["discovery"]
    if discovery["last_message"]:
        print(f"\nFactory: {discovery['last_message']}")
    for question in discovery["questions"]:
        print(f"\n- {question['question']}\n  {question['why']}")
        if question["recommendation"]:
            print(f"  Recomendación: {question['recommendation']}")
    details = {d["id"]: d for d in discovery["decision_details"]}
    for decision in snapshot["decisions"]:
        if decision["answer"] is None:
            print(f"\nDecisión {decision['id']}: {decision['question']}")
            if decision["id"] in details:
                detail = details[decision["id"]]
                print(f"  {detail['why']}")
                if detail["recommendation"]:
                    print(f"  Recomendación: {detail['recommendation']}")
    if snapshot["phase"] != "discovery":
        print(f"\nDiscovery completado. Fase actual: {snapshot['phase']}. Arquitectura aún no implementada.")


def converse(store, *, message=None, once=False, model=None):
    from .codex_discovery import CodexDiscovery
    from .discovery import Discovery

    store.initialize()
    engine = Discovery(store, model if model is not None else CodexDiscovery())
    snapshot = store.snapshot()
    if snapshot["phase"] != "discovery":
        raise WorkflowError(f"Project is in {snapshot['phase']}; discovery cannot run again")
    if message is not None:
        snapshot = engine.submit(message)
    elif snapshot["discovery"]["pending_turn"] is not None:
        print("Reintentando la entrada guardada…", flush=True)
        snapshot = engine.resume()
    show_discovery(snapshot)
    if once or snapshot["phase"] != "discovery":
        return
    print("\nDescribe tu idea o responde directamente. /estado, /salir. EOF también guarda la sesión.")
    while snapshot["phase"] == "discovery":
        try:
            answer = input("\nTú: ")
        except EOFError:
            print("\nSesión guardada.")
            return
        if answer.strip() == "/salir":
            print("Sesión guardada.")
            return
        if answer.strip() == "/estado":
            snapshot = store.snapshot()
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))
            continue
        if not answer.strip():
            continue
        print("Factory está incorporando tu respuesta…", flush=True)
        snapshot = engine.submit(answer)
        show_discovery(snapshot)


def main():
    parser = argparse.ArgumentParser(description="Local deterministic software factory")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "next", "discovery", "skills", "route-skills"):
        command = commands.add_parser(name)
        command.add_argument("project", nargs="?", default=".")
        if name == "discovery":
            command.add_argument("--message", help="Initial idea or one conversational answer")
            command.add_argument("--once", action="store_true", help="Process at most one turn and exit")
        if name in ("skills", "route-skills"):
            command.add_argument("--skill-root", action="append", default=[], help="Explicit additional skill root; repeatable")
            command.add_argument("--skills-config", help="Configuration JSON; default PROJECT/.factory/skills.json")
        if name == "route-skills":
            command.add_argument("--domain", default="general")
            command.add_argument("--intent", default="implement")
            command.add_argument("--risk", choices=("low", "normal", "high", "critical"), default="normal")
            command.add_argument("--ui", action="store_true")
            command.add_argument("--concern", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.command in ("skills", "route-skills"):
            from dataclasses import asdict
            from .skill_catalog import discover_catalog
            from .skill_router import SkillRouter, Work, load_config

            roots, policy = load_config(args.project, args.skills_config)
            catalog = discover_catalog(args.project, (*roots, *args.skill_root))
            if args.command == "skills":
                print(json.dumps(catalog.as_dict(), ensure_ascii=False, indent=2))
                return
            work = Work(args.domain, args.intent, args.risk, args.ui, tuple(args.concern))
            routing = SkillRouter(catalog, policy).route(work)
            print(json.dumps({"work": asdict(work), "routing": routing.as_dict(),
                              "catalog_errors": catalog.errors, "explicit_roots": catalog.explicit_roots},
                             ensure_ascii=False, indent=2))
            if routing.blocked:
                parser.exit(2)
            return
        store = Store(args.project)
        if args.command == "discovery":
            converse(store, message=args.message, once=args.once)
            return
        if args.command == "init":
            store.initialize()
        snapshot = store.snapshot()
        result = next_action(snapshot) if args.command == "next" else snapshot
    except (WorkflowError, sqlite3.Error, OSError) as exc:
        parser.exit(1, f"factory: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "\nSesión guardada; una entrada pendiente se reintentará al volver a ejecutar discovery.\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
