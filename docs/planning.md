# Progressive planning

Factory transforma los conceptos activos de discovery y la baseline aceptada en un roadmap
versionado. No crea Tasks, código de producto, workers de ejecución ni infraestructura de
harness. El proceso autónomo atraviesa `discovery → architecture → planning → execution`
y se detiene con `implementation_boundary` antes de implementar. El [motor del paso 8](execution.md)
requiere una autorización explícita independiente. Por defecto ejecuta una slice preparada;
la [continuidad](continuation.md) añade refinamiento, ejecución secuencial y cierre integrado. El salto entre milestones requiere `continuation.inter_milestone=true` y conserva los límites del run.

## Contrato

`factory/planning_contract.py` define un JSON Schema cerrado compartido por el SDK y Python.

- **Milestone:** capacidad/estado significativo; objetivo, requisitos, criterios de éxito,
  dependencias, riesgos, resultado observable, condiciones de cierre, binding arquitectónico,
  gates y estado abierto/completado. No equivale a una feature individual.
- **Slice:** resultado vertical verificable, milestone propietario, requisitos, dependencias,
  scope/exclusiones, aceptación, componentes/boundaries, riesgos, impacto arquitectónico,
  expectativas/triggers de verificación y madurez. `risk_probe` permite retirar incertidumbre.
- **Madurez:** `outline`, `ready_for_refinement`, `execution_ready`, `completed`.
  `near_term` contiene como máximo tres slices; solo estas pueden estar listas para ejecución.
  Las futuras conservan objetivo, ownership y dependencias sin exigir detalle de aceptación.
  El orden de las listas desempata entre opciones; las dependencias gobiernan elegibilidad.
- **Binding:** cada elemento guarda revisión y fingerprint SHA-256 de la baseline utilizada.
  `none`, `expected_within_baseline` y `potential_change` describen impacto esperado. La última
  opción registra una señal; no cambia arquitectura ni dispara su revisión.

El límite del contrato es de 100 milestones y 200 slices, no un objetivo de generación.
Las instrucciones piden mantener un roadmap compacto, sin cientos de tareas detalladas.

## Cobertura y calidad

Los conceptos activos existentes son las identidades de requisitos, restricciones y
exclusiones que planning debe conservar. Cada uno tiene exactamente un registro de cobertura:
`covered`, `deferred`, `out_of_scope` o `blocked`, siempre con milestone propietario y
justificación. Un requisito cubierto apunta a una o varias slices del milestone. Python
comprueba referencias y trazabilidad en ambos sentidos. Estar planificado no equivale a estar
completado; la consulta de requisitos distingue cobertura de trabajo pendiente.

El gate exige criterios de éxito/cierre, ownership total, bindings válidos, ausencia de ciclos
incluidos ciclos entre dependencias de slices y milestones, al menos una slice próxima
execution-ready elegible, coherencia de gates/harness, riesgos de baseline conservados,
mitigación o aceptación y validación temprana de riesgos críticos, y ausencia de decisiones
pendientes o blockers. No permite una transición manual para saltarlo.

Los riesgos tienen owner, mitigación o aceptación humana, slice de validación y slices que
bloquean. Las dependientes deben ordenar la validación como prerrequisito. Un riesgo crítico
sin aceptación requiere validación en el horizonte próximo. No se puede bajar silenciosamente
la severidad de riesgos de la baseline.

La suficiencia semántica de una slice, el tamaño apropiado de un milestone y la calidad de
los checks siguen siendo responsabilidad del planner/reviewer; el gate no pretende demostrar
que el plan es perfecto ni interpretar texto libre como prueba de corrección.

## Verificación y harness

Los gates son registros explícitos: ID, tipo, trigger, target, checks, infraestructura requerida,
coste, señales y justificación.

| Tipo | Momento | Coste y uso |
| --- | --- | --- |
| `local` | `after_slice` | Barato; comportamiento afectado, invariantes, lint/types de alcance apropiado |
| `integration` | `before_slice` / `after_slice` | Boundary, varios componentes, persistencia, seguridad o API pública |
| `milestone` | `milestone_close` | Criterios del milestone completo |
| `system` | `project_checkpoint` sobre un milestone | Punto estratégico explícito, por ejemplo release |

Cada slice execution-ready requiere verificación local. Cada milestone tiene gate de cierre.
Integration se exige cuando la slice declara uno de sus triggers estructurales; system no se
impone automáticamente. Un gate local no puede etiquetarse como caro. El reviewer juzga si
la selección de checks es proporcional; el código valida su estructura y referencias.

Cada necesidad de harness registra capability, motivo, slice/milestone que la introduce,
`before_slice` o `during_slice` y gates consumidores. Las referencias se validan en ambos
sentidos. La introducción debe ocurrir antes del consumo según las dependencias, o durante
la misma slice si el gate es posterior. No se instala ni construye nada en esta fase.

## Workers, contexto y decisiones

El planner recibe snapshots de requisitos activos, baseline vigente, ADRs, restricciones,
riesgos, criterios y decisiones. No recibe transcript, eventos, tablas SQLite, historial de
arquitectura ni catálogo completo. Fuente: máximo 100.000 bytes; contexto de llamada: 180.000;
salida: 130.000. Se rechaza un exceso de forma explícita, nunca se truncan hechos.

`SkillRouter` selecciona planning y specification del catálogo real; skills required usan
inputs nativos del SDK y las recomendaciones son pocas y opcionales. No hay agente selector.
El adaptador usa hilos efímeros independientes, sandbox read-only y login Codex/ChatGPT.

`refinement_snapshot` describe el contexto de refinamiento con slice/milestone, arquitectura actual
(y binding anterior para detectar deriva), elementos/ADRs relevantes, dependencias, requisitos
pendientes, riesgos, gates/harness y evidencia suministrada explícitamente. El controller de
continuidad ejecuta el refinador aislado y publica revisiones por slice separadas del roadmap;
la consulta por sí misma nunca llama al modelo ni inicia replanning.

Solo decisiones de alcance, experiencia, arquitectura, coste significativo, dependencia o
tradeoff relevante se elevan al humano. `factory_answer` persiste la respuesta literal y
reanuda automáticamente salvo pausa guardada. Las decisiones deben incorporarse mediante
sus claves en el plan. El gate comprueba esa referencia, no interpreta consentimiento libre.

La revisión independiente se omite solo para la clasificación determinista conservadora de
producto pequeño, personal, offline, sin integraciones ni riesgo/estructura significativa.
Para el resto: máximo dos llamadas de critic, una reconciliación lógica y ocho llamadas totales,
incluidas fallidas/interrumpidas. Una reconciliación puede pausarse para una decisión humana,
sin reiniciar el presupuesto. Discrepancias relevantes persistentes se registran como blockers.
Resume no reinicia esos límites ni el debate. Desbloquear un esfuerzo agotado mediante una
nueva revisión explícita queda para un controller posterior.

## Persistencia y recuperación

SQLite v5 añade `planning_run`, `planning_calls`, `planning_decisions`,
`planning_revisions` y `planning_current`. Los documentos JSON estructurados dentro de SQLite
son autoritativos. Cada revisión completa guarda parent, binding, fingerprint del plan,
snapshot de entrada, revisión crítica, gate, motivo y proyección. Triggers impiden UPDATE/DELETE
de revisiones; `append_revision` exige parent esperado y protege elementos completados y la
pertenencia a milestones cerrados. Es una primitiva interna para el siguiente controller,
no una tool para saltar gates ni un algoritmo de replanning.

Cada llamada se registra antes del SDK. Los checkpoints y eventos se confirman atómicamente,
con revisión optimista y lock de proceso. Los intentos interrumpidos conservan presupuesto.
La aceptación del roadmap, estrategia de gates/harness, cierre y transición son una sola
transacción. `.factory/planning/ROADMAP.md` se exporta desde la proyección guardada: una caída
entre commit y escritura se recupera con `planning`, sin repetir llamadas Codex. La proyección
es reparable y nunca alimenta el runtime.

## Consultas

`factory_inspect(view=...)` conserva estas vistas de planning; ejecución añade dos tools:

| Pregunta | View |
| --- | --- |
| ¿Cuál es el plan? | `plan` |
| ¿Qué milestones hay? | `milestones` |
| ¿Cuál es la próxima slice? | `next_slice` |
| ¿Qué requisitos siguen pendientes? | `requirements` |
| ¿Dónde están los gates y cuándo se crea harness? | `verification` |
| Contexto para refinar una slice | `refinement`, con `slice_id` |

`factory_decisions` responde qué necesita decidir el humano. `status` mantiene progreso
compacto y revisión actual; las vistas muestran si un plan es borrador o aceptado. La próxima
slice se devuelve solo para un roadmap aceptado. `implementation_enabled` refleja la
autorización explícita; las dependencias se resuelven con recibos de aceptación de Factory.

CLI secundaria: `planning`, `planning-show --view plan|milestones|next_slice|requirements|verification|markdown`.

La suite usa dobles sin cuota: `.venv/bin/python -m unittest discover -s tests -v`.
El smoke real es opt-in: `.venv/bin/python scripts/smoke_planning.py`; crea un proyecto temporal,
usa Codex real y conserva artefactos para inspección, sin ejecutar código de producto.

Para aislar el adaptador nuevo sin decisiones de discovery/architecture del ejemplo:
`.venv/bin/python scripts/smoke_planning.py --planning-only` usa fixtures deterministas
para las fases anteriores y Codex real exclusivamente para planning.
