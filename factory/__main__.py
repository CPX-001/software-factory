"""Local factory CLI, including resumable conversational discovery."""

import argparse
import json
import sqlite3

from .workflow import WorkflowError
from .application import FactoryService


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
        print(f"\nDiscovery completado. Fase actual: {snapshot['phase']}. Ejecuta architecture para continuar.")


def converse(store, *, message=None, once=False, model=None):
    service = store if isinstance(store, FactoryService) else FactoryService.for_local(store.project, discovery_model=model)
    snapshot = service.initialize_local()
    if snapshot["phase"] != "discovery":
        raise WorkflowError(f"Project is in {snapshot['phase']}; discovery cannot run again")
    if message is not None:
        snapshot = service.discovery_turn(message)
    elif snapshot["discovery"]["pending_turn"] is not None:
        print("Reintentando la entrada guardada…", flush=True)
        snapshot = service.discovery_turn()
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
            snapshot = service.snapshot()
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))
            continue
        if not answer.strip():
            continue
        print("Factory está incorporando tu respuesta…", flush=True)
        snapshot = service.discovery_turn(answer)
        show_discovery(snapshot)


def main():
    parser = argparse.ArgumentParser(description="Local deterministic software factory")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "message", "process", "status", "next", "discovery", "skills", "route-skills", "architecture", "architecture-show", "architecture-status", "architecture-adrs", "answer", "pause", "resume", "allow-root", "planning", "planning-show", "execution-policy", "execute", "execution-show", "validate-project", "project-validation-show"):
        command = commands.add_parser(name)
        command.add_argument("project", nargs="?", default=".")
        if name == 'init':
            command.add_argument('--workflow', choices=('adaptive', 'verified'), default='adaptive')
        if name == 'message':
            command.add_argument('--message', required=True)
            command.add_argument('--request-id')
        if name == 'execution-policy':
            command.add_argument('--policy', required=True, help='Reviewed JSON policy file')
            command.add_argument('--verification', required=True, help='Typed verification JSON file')
        if name in ('execute', 'validate-project'):
            command.add_argument('--request-id', required=True)
        if name == 'validate-project':
            command.add_argument('--automatic-remediation', action='store_true')
        if name == "planning-show":
            command.add_argument("--view", choices=("plan", "milestones", "next_slice", "requirements", "verification", "markdown"), default="plan")
        if name == "architecture-show":
            command.add_argument("--markdown", action="store_true", help="Render the persisted human projection")
        if name == "answer":
            command.add_argument("--id", type=int, required=True)
            command.add_argument("--answer", required=True)
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
        service = FactoryService.for_local(args.project)
        if args.command in ("skills", "route-skills"):
            from .skill_router import Work
            work = (Work(args.domain, args.intent, args.risk, args.ui, tuple(args.concern))
                    if args.command == "route-skills" else None)
            result = service.skill_catalog(roots=args.skill_root, config=args.skills_config, work=work)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if work and result["routing"]["status"] == "blocked":
                parser.exit(2)
            return
        if args.command == 'message':
            result = service.submit_user_message(args.message, request_id=args.request_id)
        elif args.command == 'process':
            result = service.inspect(view='process')
        elif args.command == 'execution-policy':
            from pathlib import Path
            result = service.configure_execution(json.loads(Path(args.policy).read_text()),
                                                 json.loads(Path(args.verification).read_text()))
        elif args.command == 'execute':
            result = service.execute_next_slice(request_id=args.request_id)
        elif args.command == 'validate-project':
            result = service.validate_project(request_id=args.request_id, automatic_remediation=args.automatic_remediation)
        elif args.command == 'project-validation-show':
            result = service.inspect(view='project_validation')
        elif args.command == 'execution-show':
            result = service.inspect(view='execution')
        elif args.command == "allow-root":
            result = service.authorize_root(args.project)
        elif args.command == "discovery":
            converse(service, message=args.message, once=args.once)
            return
        elif args.command == "planning":
            result = service.run_planning()["planning"]
        elif args.command == "planning-show":
            result = service.get_planning(view=args.view)
            if args.view == "markdown":
                print(result or "No accepted roadmap", end="")
                return
        elif args.command == "architecture":
            result = service.run_architecture()["architecture"]
        elif args.command == "answer":
            service.answer_decision(args.id, args.answer, continue_run=False)
            result = service.snapshot()
        elif args.command == "init":
            service.initialize_project(workflow=args.workflow)
            result = service.snapshot()
        elif args.command == "architecture-show":
            result = service.get_architecture()
            if result.get("baseline", True) is None:
                raise WorkflowError("There is no accepted architectural baseline; consult architecture-status")
            if args.markdown:
                print(service.get_architecture(view="markdown"), end="")
                return
        elif args.command == "architecture-adrs":
            result = service.get_architecture(view="adrs")
        elif args.command == "architecture-status":
            result = service.architecture_diagnostics()
        elif args.command == "pause":
            result = service.pause()
        elif args.command == "resume":
            result = service.resume()
        elif args.command == "next":
            result = service.get_next_action()
        else:
            result = service.get_status() if service.snapshot()['phase'] == 'execution' else service.snapshot()
    except (WorkflowError, sqlite3.Error, OSError) as exc:
        parser.exit(1, f"factory: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "\nSesión guardada; reanuda discovery o architecture con su comando correspondiente.\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
