"""Deterministic semantic model fixtures; never invoke the SDK."""

from copy import deepcopy

from factory.discovery_contract import CRITERIA


def item(key, text=None, status="known", blocking=False):
    return {"key": key, "category": key, "text": text or key, "status": status,
            "blocking": blocking, "basis": "Explicit user input in the test scenario"}


def question(key="audience"):
    return {"key": key, "question": "¿Quién lo usará primero?", "why": "Determina el flujo principal.",
            "recommendation": "Empezar por fundadores individuales para limitar el MVP."}


def reply(*, knowledge=None, ready=False, questions=None, decisions=None):
    return {"message": "He incorporado lo indicado.", "knowledge": knowledge or [],
            "questions": [question()] if questions is None and not ready else (questions or []),
            "resolve_questions": [], "decisions": decisions or [], "decision_answers": [],
            "assessment": {"ready": ready, "reason": "Evaluación del escenario de prueba.",
                           "evidence": []}}


def complete_reply():
    facts = {
        "vision": "SaaS que encuentra oportunidades de producto en repositorios GitHub.",
        "users": "Fundadores individuales buscando su siguiente producto.",
        "problem": "Leer manualmente issues para identificar demanda es lento.",
        "capabilities": "Pegar URL pública, analizar issues y mostrar oportunidades con citas a los issues.",
        "scope": "MVP con análisis solicitado de un repositorio por vez y resultados privados por usuario.",
        "success": "El fundador encuentra una oportunidad respaldada por tres issues distintos.",
        "out_of_scope": "Repositorios privados, pagos y análisis continuo quedan fuera del MVP.",
        "constraints": "Despliegue web económico, presupuesto operativo de 100 euros al mes.",
        "scale": "100 usuarios iniciales, 10 análisis diarios por usuario; procesamiento asíncrono de minutos.",
        "security": "Solo datos públicos; autenticación de usuarios y aislamiento de resultados por propietario.",
        "integrations": "API pública de GitHub, con sus límites de acceso; sin escritura en repositorios.",
    }
    result = reply(knowledge=[item(key, value) for key, value in facts.items()], ready=True)
    result["assessment"]["evidence"] = [{"criterion": key, "keys": [key]} for key in CRITERIA]
    return result


class FakeModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.contexts = []

    def respond(self, context):
        self.contexts.append(deepcopy(context))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return deepcopy(response)
