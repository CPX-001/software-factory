# Routing de skills

El router selecciona herramientas de trabajo; no crea agentes, no ejecuta skills ni cambia
el workflow. Discovery sigue siendo independiente; el handler de architecture consume este routing.
Solo el descubrimiento consulta el runtime local. El routing posterior es una función
determinista que puede reutilizar el mismo catálogo en muchas unidades de trabajo.

## Uso

Desde el repositorio, usando el entorno del SDK existente:

```bash
.venv/bin/python -m factory skills .
.venv/bin/python -m factory route-skills . --domain frontend --intent design --concern design-system
.venv/bin/python -m factory route-skills . --domain architecture --intent diagram
.venv/bin/python -m factory route-skills . --intent debug
.venv/bin/python -m factory route-skills . --risk critical --concern security
```

Estos comandos no requieren inicializar SQLite ni modifican su estado. Devuelven JSON:
catálogo con IDs, rutas, ámbito, procedencia, habilitación y errores; o tarea, selección,
motivos, alternativas no disponibles, reglas descartadas y contexto compacto.
Un bloqueo por requisitos sale con código **2** y `routing.status: "blocked"`.
Los errores de configuración salen con código 1. Una recomendación ausente no bloquea.
Los errores al consultar el catálogo se muestran en `catalog_errors` (o `errors` en
`skills`); un fallo de descubrimiento no se presenta como una lista completa vacía.

## Características semánticas

`Work(domain, intent, risk, ui, concerns)` evita que los planners conozcan los nombres de
las skills. `risk` admite `low`, `normal`, `high`, `critical`; `ui` es booleano;
`concerns` es una colección de etiquetas, por ejemplo `security`, `testing`, `performance`,
`accessibility`, `design-system`, `architecture`. El orden y los duplicados no influyen.
Los dominios `frontend` y `ux` implican UI. Las etiquetas son extensibles; una etiqueta
sin regla no inventa una recomendación por parecido textual.

La CLI permite repetir `--concern`. No utiliza búsqueda de palabras en el enunciado
libre, embeddings ni un turno LLM para clasificar tareas.

## Descubrimiento: runtime y configuración explícita

`factory/skill_catalog.py` usa el SDK oficial instalado para consultar
[`skills/list`](https://learn.chatgpt.com/docs/app-server#skills), con el directorio real
del proyecto y `forceReload: true`. Conserva `enabled`, errores y ámbito del runtime.
No mantiene un inventario manual ni infiere habilitación a partir de una caché.
La consulta corre en un proceso acotado a 45 segundos, sin `thread/start`, `turn/start`,
login ni claves API. Los tests usan respuestas simuladas y no necesitan instalar el SDK.

Las instalaciones de Windows y WSL pueden exponer catálogos distintos. Para incorporar
una ubicación externa se proporciona **explícitamente** `--skill-root`, repetible:

```bash
.venv/bin/python -m factory skills . --skill-root /mnt/c/Users/kiril/.codex/skills
```

La ruta del ejemplo fue inspeccionada en este entorno. En otro equipo se debe usar una
ubicación realmente existente. El runtime registra estas raíces mediante
`skills/extraRoots/set` en esa conexión; no se copian skills, credenciales ni se modifica
la configuración global de Codex. El runtime interpreta SKILL.md y sus metadatos; la
factory no incorpora otro parser YAML ni recorre indiscriminadamente instalaciones ajenas.
Añadir una raíz de caché explícitamente hace sus archivos candidatos a carga en esta
sesión; no prueba que estén instalados los conectores o servicios que puedan requerir.

Para este proyecto quedaron guardadas las ubicaciones verificadas en
`.factory/skills.json`, ignorado por Git: skills personales de Windows y sus Agent Skills,
más las ubicaciones locales de Frontend Design Premium, Product Design y complex-enough.
Los directorios de paquete configurados no fijan una versión concreta. Sin ese archivo,
el comportamiento portable por defecto es exclusivamente el catálogo nativo del runtime.

Cuando existe un manifiesto `.codex-plugin/plugin.json` en un antecesor de la ruta
**devuelta por el runtime**, su nombre da el prefijo `paquete:skill`. El manifiesto sirve
para identificar, nunca para declarar habilitación. Una instalación independiente mantiene
su nombre original. Las copias idénticas se deduplican por ID y ruta real. Si hay varias
copias activas con el mismo ID, o un nombre corto es ambiguo, no se elige una arbitrariamente:
se informa `ambiguous`. Usa un ID cualificado o una raíz más concreta para resolverlo.
Una entrada deshabilitada produce `disabled`, y una ausente, `not_discovered`.

## Política inicial

`factory/skill_router.py` contiene reglas pequeñas con condiciones semánticas, alternativas
ordenadas, grupo de equivalencia, prioridad y obligatoriedad. Estos son nombres comprobados
leyendo los metadatos instalados, no identificadores inventados:

| Trabajo | Selección preferida |
| --- | --- |
| Arquitectura / contratos API | `agent-skills:api-and-interface-design` |
| Diagrama técnico | `archify` |
| Registrar ADR | `agent-skills:documentation-and-adrs` |
| UI nueva | `frontend-design-premium:frontend-design-premium`, con su dependencia `frontend-design` |
| Sistema de diseño explícito | `design-system` |
| Revisión visual | `reviewing-design-work` |
| Consistencia con el sistema existente | `design-system-consistency` |
| Accesibilidad renderizada | `frontend-a11y` |
| Auditoría UX | `product-design:audit` |
| Alternativas visuales | `product-design:ideate` |
| Implementar una referencia | `product-design:image-to-code` / `product-design:url-to-code`, según intención |
| Debugging | `agent-skills:debugging-and-error-recovery` |
| Testing / TDD | `agent-skills:test-driven-development` |
| Seguridad | `agent-skills:security-and-hardening` |
| Rendimiento | `agent-skills:performance-optimization` |
| Refactor / simplificación | `agent-skills:code-simplification` |
| Revisión de código | `agent-skills:code-review-and-quality` |
| Planificación / especificación | `agent-skills:planning-and-task-breakdown` / `agent-skills:spec-driven-development` |
| Git / CI explícitos | `agent-skills:git-workflow-and-versioning` / `agent-skills:ci-cd-and-automation` |

No se encontró una skill independiente llamada «arquitectura general» dentro de Agent
Skills: la política utiliza su skill real de contratos y límites de módulos. No selecciona
Git, TDD, seguridad o coordinación simplemente porque exista código en una tarea.

Las especializadas solo se sugieren ante su preocupación explícita: `contrast-apca`,
`contrast-wcag`, `color-space`, `palette`, `typography`, `line-height`, `spacing`,
`component-sizing`, `tokens-dtcg`, `token-naming`, `brand`, `banner`. APCA y WCAG no se
activan juntas por defecto. Las etiquetas `delegation`, `agent-qa` y `panel` permiten
seleccionar las skills de coordinación verificadas, sin ejecutar delegación ni incorporar
un sistema de agentes. El riesgo alto por sí solo no activa un panel.

## Required, recommended y redundancia

- `required` expresa una política exigible. Por defecto, **seguridad + riesgo crítico**
  exige `security-and-hardening`. Si ninguna alternativa válida está disponible, se
  informa un bloqueo y no se genera contexto de ejecución. Nunca se afirma que se usó.
- `recommended` es una ayuda opcional. Una skill ausente, deshabilitada, excluida o ambigua
  se registra y se prueba la siguiente alternativa. Si no hay ninguna, se continúa.
- Los requisitos se resuelven primero y nunca se recortan por presupuesto. Las recomendaciones
  tienen un límite inicial de **3 skills**, configurable entre 0 y 5. Se deduplican por
  identidad resuelta y se admite un solo enfoque por grupo equivalente.
- Las alternativas de diseño (`frontend-design-premium`, `ui-ux-pro-max`, `frontend-design`,
  `design`, `ui-styling`) son sustitutas, no una lista para activar en bloque. El sistema
  de diseño o la accesibilidad se añaden solo cuando la tarea lo expresa.
- Premium exige realmente leer la skill upstream `frontend-design`: esa dependencia cuenta
  dentro del límite. Si falta, se descarta Premium y se intenta otra opción; si Premium era
  obligatoria, la falta de su dependencia bloquea. La excepción es composición documentada,
  no redundancia accidental. Las skills cargadas conservan sus propias instrucciones.
- Los requisitos explícitos del proyecto se respetan incluso si piden dos enfoques del mismo
  grupo. Una recomendación no puede desplazar ni debilitar un requisito.

## Overrides por proyecto

Se lee `.factory/skills.json` o el archivo indicado mediante `--skills-config`.
Ejemplo de estructura, con una raíz relativa que debe existir en tu equipo:

```json
{
  "roots": ["../shared-skills"],
  "policy": {
    "max_recommended": 2,
    "disabled_rules": ["git"],
    "bindings": {"ui-design": ["ui-ux-pro-max", "frontend-design"]},
    "required": ["agent-skills:security-and-hardening"],
    "recommended": [],
    "exclude": []
  }
}
```

`bindings` sustituye las alternativas de una regla, manteniendo su carácter requerido o
recomendado. `disabled_rules` es una desactivación explícita; una lista vacía en un binding
se rechaza para no desactivar silenciosamente una política. `required` / `recommended`
agregan selectores del proyecto; `exclude` filtra disponibilidad. Excluir una skill requerida
produce bloqueo. Las claves o reglas desconocidas y los tipos incorrectos se rechazan.
Las raíces relativas se resuelven respecto al directorio del proyecto.

## Integración con Codex

```python
from factory.skill_catalog import discover_catalog
from factory.skill_router import SkillRouter, Work, load_config

roots, policy = load_config(project)
catalog = discover_catalog(project, roots)
router = SkillRouter(catalog, policy)  # reutilizable para muchas tareas
routing = router.route(Work(domain="backend", risk="critical", concerns=("security",)))
context = routing.context()  # falla si hay requisitos sin cubrir
required_inputs = routing.required_inputs(catalog)
```

`context()` incluye únicamente nombres requeridos, recomendados y una indicación de que
Codex puede descubrir otras skills útiles. No incluye catálogo completo, descripciones ni
contenido SKILL.md. `required_inputs()` produce los objetos `{type: "skill", name, path}`
del protocolo de Codex únicamente para requisitos; las recomendaciones siguen siendo pistas.
El método tampoco ejecuta nada y rechaza un catálogo posterior que haya perdido un requisito.

El ejecutor debe usar el mismo directorio y las mismas raíces explícitas al consultar
skills en su conexión, o adjuntar los inputs por ruta. El registro de raíces es local a la
conexión y no se debe asumir que persiste entre procesos. Las dependencias externas de las
skills (navegador, conectores, etc.) se resuelven al ejecutarlas; este router verifica la
disponibilidad de las skills, no instala sus herramientas.

No hay cambios de esquema SQLite, invocaciones LLM, ejecución de arquitectura ni pruebas
reales que consuman cuota para seleccionar skills.

Architecture adjunta las required por ruta mediante `SkillInput` y deja recommended como
candidatas. Consulta [el flujo de architecture](architecture.md) para sus checkpoints y gates.
