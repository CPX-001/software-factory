# Software Factory

La factory convierte una idea incompleta en conocimiento estructurado suficiente para
producir una baseline arquitectónica estructurada y versionada. Codex conduce discovery
y diseña/revisa arquitectura; Python valida, persiste y controla los gates y transiciones.
Planning produce un roadmap progresivo versionado con gates de cobertura/verificación y necesidades de harness. El proceso se detiene en `execution` antes de implementar producto.

La interfaz principal es Codex App mediante el plugin local y MCP. Consulta
[instalación y uso desde Codex](docs/codex.md). La CLI se conserva para recovery, debugging
y scripting, sobre el mismo `FactoryService`.

Consulta también [el contrato y gate de arquitectura](docs/architecture.md) y [progressive planning](docs/planning.md).

## Probar una conversación

Desde este repositorio, usa el entorno donde ya funciona el SDK oficial de Codex:

```bash
source .venv/bin/activate
python3 -m factory discovery /ruta/al/proyecto --message "Quiero hacer un SaaS que analiza repositorios GitHub y encuentra oportunidades de producto."
```

El directorio del proyecto debe existir; discovery inicializa `.factory/` si hace falta.
Sin ruta se usa el directorio actual. Responde directamente a las preguntas: cada respuesta
inicia la siguiente interacción sin escribir «continúa». `/estado` muestra el conocimiento
estructurado. `/salir`, Ctrl-D o Ctrl-C cierran la sesión conservando lo confirmado.

Para reanudar, también después de cerrar el proceso:

```bash
python3 -m factory discovery /ruta/al/proyecto
```

La reanudación muestra el último mensaje y las preguntas/decisiones pendientes sin gastar
cuota. Si una llamada falló o fue interrumpida, recupera y reintenta la entrada ya guardada.
No envíes una entrada nueva con `--message` hasta procesar la pendiente. No hay reintentos
automáticos del controlador ante un error. Una interrupción después de terminar el modelo
pero antes del commit puede requerir repetir esa llamada, pero no duplica cambios de estado.

Para procesar una sola interacción, o consultar estado sin invocar Codex:

```bash
python3 -m factory discovery /ruta/al/proyecto --message "El usuario inicial será un fundador individual" --once
python3 -m factory status /ruta/al/proyecto
python3 -m factory next /ruta/al/proyecto
python3 -m factory init /ruta/al/proyecto
```

`status` y `next` siguen siendo consultas JSON sin efectos. `next` devuelve `discovery`
con su readiness y preguntas, `wait_for_human` con IDs cuando hay decisiones pendientes,
o `architecture` con `implemented: true`, etapa y bloqueos al finalizar. `init` es idempotente.

## Diseño y contrato

- `factory/discovery_contract.py`: instrucciones de conversación, JSON Schema cerrado,
  límites y validación local estricta, sin depender de parsear prosa o Markdown.
- `factory/discovery.py`: conocimiento por conceptos con claves estables, actualizaciones
  incrementales, cola durable de entrada, resolución de preguntas y gate determinista.
- `factory/codex_discovery.py`: adaptador del SDK oficial `openai_codex` instalado localmente.
- `factory/workflow.py`: SQLite por proyecto, revisión optimista, eventos y controlador puro.

La salida interna separa `message`, `knowledge`, `questions`/`resolve_questions`,
`decisions`/`decision_answers` y `assessment`. Se permiten como máximo tres nuevas
preguntas/decisiones por interacción, preferiblemente una o dos. Las instrucciones piden
priorizar incertidumbres que cambian producto, requisitos, alcance o diseño; reutilizar
lo conocido y hacer inferencias reversibles explícitas, sin convertir el gate en un cuestionario.

Cada concepto tiene categoría, texto, certeza (`known`, `assumption`, `unknown`,
`conflict` o `superseded`), indicador de bloqueo y fundamento. Las categorías cubren visión,
usuarios, problema, capacidades, éxito observable, alcance/exclusiones, restricciones,
preferencias técnicas, requisitos no funcionales, escala, seguridad, integraciones y decisiones.
Los conceptos se actualizan por clave; los sustituidos quedan archivados y fuera del contexto.

Las decisiones humanas pendientes tienen identidad estable y bloquean el avance. Una
respuesta natural puede resolverlas: el modelo debe identificar el ID y citar literalmente
el fragmento de la **última entrada** que responde. El código comprueba que la decisión siga
pendiente y que la cita exista. La interpretación semántica corresponde al modelo; no se
considera una sugerencia suya como una respuesta humana. Las respuestas también se guardan
como conocimiento protegido para conservarlas sin reenviar el historial de decisiones.
La API previa `Store.request_decision` / `Store.answer_decision` sigue disponible.

## Contexto acotado y autenticación

Cada interacción crea un hilo **nuevo y efímero** del SDK, con directorio temporal,
sandbox de solo lectura y aprobaciones denegadas. Recibe instrucciones estables, conceptos
activos, preguntas/decisiones pendientes, readiness anterior y la última entrada del usuario.
Nunca reanuda un hilo de Codex ni envía eventos o transcript completo. No necesita leer
archivos del proyecto; las instrucciones prohíben usar herramientas durante discovery.

El contexto de datos tiene un máximo de 60.000 bytes UTF-8 y 100 conceptos activos;
las instrucciones y el esquema son de tamaño fijo. Cada mensaje admite 8.000 caracteres
y 16.000 bytes al codificarlo como JSON. Se reserva espacio para la siguiente entrada antes de confirmar cambios.
Los límites rechazan la operación en lugar de truncar hechos silenciosamente; los conceptos
obsoletos pueden ser sustituidos/consolidados por el modelo en su actualización incremental.
El historial de auditoría puede crecer en disco, pero no se envía al modelo.

El adaptador usa `gpt-5.6-terra`, el modelo del smoke existente, mediante el runtime
incluido en el SDK y la autenticación Codex/ChatGPT actual. Fuerza proveedor OpenAI y login
ChatGPT, verifica el tipo de cuenta y neutraliza `OPENAI_API_KEY` / `CODEX_API_KEY` en el
proceso hijo. No introduce cliente de la API de OpenAI, login con claves ni fallback.
Si falta el SDK, usa `.venv/bin/python`; `requirements.txt` fija la versión ya validada.

## Readiness y workflow

El gate exige evidencias con referencias a conceptos activos para estas categorías:

| Información necesaria | Certeza aceptada |
| --- | --- |
| Visión, usuarios, problema, capacidades y alcance del MVP | Conocida por aportaciones humanas |
| Éxito observable, exclusiones, restricciones, escala, seguridad e integraciones | Conocida o supuesto reversible explícito y no bloqueante |

Las instrucciones detallan la profundidad esperada: recorrido principal y salidas,
fronteras del MVP, orden de magnitud de carga, sensibilidad de datos y acceso, integración
y restricciones relevantes. No se exige elegir versiones, librerías o todos los detalles
antes de arquitectura. Una ausencia justificada puede cubrir una integración o restricción.

Python exige **todas** las categorías, comprueba referencias, categoría y certeza, y rechaza
readiness si queda cualquier conflicto, concepto bloqueante, pregunta pendiente o decisión
sin responder. Además requiere la evaluación semántica favorable del modelo. Un `ready: true`
aislado no permite avanzar, ni puede saltarse el gate con `Store.transition`.
La validación de cobertura es determinista; la suficiencia semántica de los hechos y la
detección de contradicciones son tareas de Codex, no una garantía formal del esquema.

Al superar el gate se guardan evaluación, evidencias, resultado del gate, fecha de
finalización y evento de transición **en la misma transacción** que el conocimiento final:

```text
discovery -> architecture -> planning -> execution -> verification -> completed
                                           ^              |
                                           +--------------+
```

`requirements` se conserva únicamente para poder leer y avanzar proyectos antiguos que
ya estuvieran en esa fase. El nuevo discovery reúne la información de producto y requisitos
y pasa directamente a arquitectura. Architecture y planning tienen handlers y gates propios; execution y las fases posteriores aún no se implementan.

## Persistencia y recuperación

Fuente autoritativa: `.factory/state.sqlite3`, esquema v5. La migración aditiva desde v1/v2/v3/v4
ocurre en `init` o la siguiente escritura y conserva fase, revisión, eventos y decisiones.
Las lecturas de v1 siguen funcionando sin migrarlo; versiones desconocidas se rechazan.

Tablas originales: `workflow`, `decisions`, `events`. Tablas nuevas:

- `discovery_items`: conceptos estructurados actuales, incluidos los sustituidos.
- `discovery_questions`: preguntas con motivo, recomendación y resolución.
- `discovery_decisions`: vínculo de claves estables y recomendaciones a decisiones humanas.
- `discovery_turns`: entrada durable, estado pendiente/completado y salida validada para auditoría.
- `discovery_meta`: evaluación, evidencias, gate y fecha de finalización.

Primero se confirma la entrada; después se invoca Codex fuera de cualquier transacción.
El resultado se valida y aplica con revisión optimista: una respuesta obsoleta no puede
pisar una decisión concurrente. Cada mutación se confirma junto con su evento, usando
`BEGIN IMMEDIATE`, claves foráneas y `synchronous=FULL`. Un fallo de validación o de SQLite
no deja conocimiento parcial ni transiciones a medias; mantiene la entrada pendiente.
No es necesario reconstruir el conocimiento reproduciendo eventos o leyendo el transcript.

Añade `.factory/` al `.gitignore` de los proyectos gestionados. La CLI no modifica su código
ni su configuración Git. Usa SQLite en almacenamiento local, preferiblemente bajo `/home`
en WSL. Discovery se consulta con `/estado` y `status`. Architecture guarda una proyección Markdown
generada desde la baseline estructurada; no se edita como fuente de verdad.

## Verificación

La lógica de dominio usa biblioteca estándar; los tests MCP utilizan su SDK oficial.
La suite mockea los workers y no consume cuota. Para ejecutar la suite sin consumir cuota:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Se prueban procesos separados, actualizaciones incrementales, preguntas y decisiones,
readiness, contradicciones, fallos y reintentos, migración, concurrencia, rollback atómico,
CLI interactiva, contexto sin transcript y el contrato del SDK mediante dobles.
`smoke.py` conserva la prueba independiente original de conectividad.

## Routing determinista de skills

La infraestructura de routing se puede consultar por separado sin ejecutar arquitectura.
El handler de architecture la integra y activa required mediante inputs nativos del SDK.
Consulta el catálogo real de Codex y selecciona pocas skills según dominio, intención,
riesgo y preocupaciones semánticas, con requisitos bloqueantes y recomendaciones opcionales:

```bash
.venv/bin/python -m factory skills .
.venv/bin/python -m factory route-skills . --domain frontend --intent design --concern design-system
.venv/bin/python -m factory route-skills . --risk critical --concern security
```

No consume llamadas LLM. La configuración local `.factory/skills.json` permite raíces
adicionales y overrides de política. Consulta [el diseño, política y ejemplos](docs/skill-routing.md)
para distinguir catálogo nativo, ubicaciones explícitas y contexto para futuras ejecuciones.
