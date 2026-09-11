# Continuidad, validación y cierre de milestones (paso 10)

`Continuation`, dentro del controller separado de `FactoryService`, reutiliza `Execution`,
`Refiner`, el journal, el runner Python/unittest y el aislamiento existente. No hay otro
orquestador ni un loop en Codex App. Desconectar MCP o cerrar la conversación no pausa un run.
No se ejecuta Factory sobre su propio repositorio: las pruebas usan productos desechables.

## Autorización desde Codex App

La autorización de una slice y la modalidad multislice previa conservan su alcance.
El salto automático entre milestones requiere **otra propiedad explícita**, deshabilitada
cuando se omite, dentro de la política autorizada mediante `factory_execution_policy`:

```json
{
  "continuation": {
    "enabled": true,
    "inter_milestone": true,
    "max_slices": 3,
    "max_calls": 9,
    "max_seconds": 900,
    "max_tokens": 50000
  }
}
```

Es un fragmento; también hacen falta los permisos, modelo/esfuerzo, límites individuales,
reserva de cuota y verificaciones tipadas de la política completa. No autoriza cambiar
criterios ni inventar oráculos. Con esos ajustes ya autorizados se puede pedir:

> Continúa el proyecto entre milestones dentro de la política autorizada y detente si necesitas una decisión.

El plugin fija el proyecto y llama una vez a `factory_execute`, reutilizando `request_id`
en reintentos de transporte. Factory no pide otro mensaje entre slices ni milestones.
La migración conserva los runs del paso 9 terminados en `milestone_ready` como checkpoints
históricos con sus contadores/deadline intactos; permiten autorizar explícitamente un nuevo
run para su cierre pendiente. Los nuevos `milestone_ready` son checkpoints de recuperación
internos del run activo, no autorizaciones para reiniciar presupuestos.
`factory_pause` impide despachos y publicación; `factory_resume` recupera el mismo run sin
reiniciar contadores o deadline. Las respuestas humanas reactivan trabajo elegible cuando
no existe una pausa. Un checkpoint requiere una nueva solicitud explícita para otro run.

## Estados y gate de cierre

| Estado | Significado |
| --- | --- |
| Aceptación de slice (`execution.checkpoint`) | Código de esa unidad aceptado, con su recibo |
| `milestone_ready` | Todas las slices aceptadas; gate integrado pendiente |
| `milestone_validating` | Comprobación del candidato integrado |
| `milestone_validation_failed` | Fallo registrado; se clasifica antes de decidir la siguiente acción |
| `validation_pending` | Falta definición, evidencia vigente o ejecución válida de un check |
| `remediating` / ejecución activa | Unidad nueva que corrige un defecto concreto |
| `waiting_decision` / `blocked` | Decisión, capacidad o propuesta impide avanzar; ver diagnóstico |
| `milestone_closed` | Recibo durable; la autorización actual no permite saltar al siguiente |
| `checkpoint` / `budget_exhausted` | Límite intencional de unidades / presupuesto agotado |
| `project_ready_for_validation` | Todos los milestones cerrados; gate final del proyecto pendiente |

Los estados transitorios se conservan en journals; el estado de validación fallida y su
historial siguen disponibles aunque el run avance a remediación. El roadmap original no
se reescribe: su estado efectivo se obtiene de recibos, nunca de `completed` escrito por
un modelo. No se implementan release, despliegue ni gate final de proyecto.

El cierre consume **todos** los gates `milestone_close` y `project_checkpoint` dirigidos al
milestone. No agrega por defecto otras suites de coste arbitrario ni sustituye esos gates
por PASS de slices anteriores. Cada `gate_checks` cubre índices de `gate.checks`.
Para checks estratégicos, `criteria` indexa la concatenación de `success_criteria` y
`closure_conditions`, en ese orden. Cada criterio y cada comprobación obligatorios necesitan
cobertura completa. Ausente, omitido, cero tests, skipped, expected failure, timeout o
capacidad no soportada nunca cuentan como PASS. Un checkpoint obligatorio de sistema se
ejecuta si es Python/unittest; un navegador, especialista u otra capacidad ausente bloquea.

Planning puede declarar `subjective_criteria` con esos mismos índices. Estos requieren un
check `human_review`, cuyo `target` describe la revisión. Factory registra una decisión con
commit, validación y criterios exactos; solo una respuesta humana explícita `accept` cuenta
como aprobación. `ambiguous`, `architecture_change`, `scope_change` u otra respuesta conservan
el bloqueo/diagnóstico. La App transmite la respuesta real; no la inventa ni la sustituye
por una declaración del worker. Las pruebas mecánicas prueban sus casos concretos, no calidad
universal ni suficiencia semántica de cualquier requisito expresado en lenguaje natural.

## Código integrado y vigencia

`factory/accepted` continúa avanzando con los recibos inmutables de ejecución y CAS de Git.
El gate fija un commit candidato y crea un worktree gestionado sobre él. Comprueba que su
árbol completo coincide con el commit; los tests montan ese código de solo lectura en el
sandbox existente. Los tests/harness congelados por aceptaciones previas no pueden alterarse.
La rama y el checkout del usuario permanecen intactos; no hay push ni despliegue.

La identidad de validación incluye commit, arquitectura y plan (revisión/fingerprint),
definición inmutable de verificaciones, criterios, decisiones aplicables, ADRs y hashes del
runner y Python. Cada resultado conserva check, criterio, gate, código, proceso y salida.
Se guarda después de cada check y se reutilizan los resultados terminados al recuperar
exactamente la misma entrada. No se requiere inferencia para validar, inspeccionar ni cerrar.

Antes del cierre se revisan otra vez fuentes, decisiones, autorización y código. Una
transacción `verify/prepare` de Git mantiene bloqueada la referencia aceptada durante la
transacción SQLite de publicación; no cambia esa referencia. Runtime y journal comprueban
el propietario y la pausa. El recibo y el único evento `milestone_closed` se graban juntos.
Un reinicio tras verificar reutiliza evidencia vigente; tras cerrar recupera el recibo antes
de preparar el siguiente. Duplicar solicitudes no duplica cierres ni avance.

Los recibos históricos conservan su commit y fuentes. Una revisión posterior del plan,
arquitectura o definición no los transforma en evidencia de la nueva versión. Incluso cuando
sus fuentes siguen vigentes, describen únicamente el commit registrado, no cualquier código
posterior. Los requisitos y milestones se inspeccionan con esta distinción explícita.

## Cobertura y remediación

Cada requisito conserva un milestone propietario, disposición y slices trazables. Sus
slices pueden aportar desde varios milestones, que también lo referencian. Cerrar uno registra
una contribución: los demás compromisos siguen pendientes. `deferred` no es satisfecho;
`out_of_scope` no se modifica para cerrar. El avance no muta discovery ni sus hechos.

Solo un contrato explícito `verification.requirement_acceptance` puede establecer satisfacción
completa: contiene `requirement`, `condition`, `milestones` y `checks`; mantiene todos los
milestones contribuyentes, requiere sus cierres y PASS de sus oráculos sobre el candidato
integrado actual. Sin ese contrato, el requisito sigue activo aunque sus contribuciones estén
verificadas. El resumen y el estado se derivan de datos y recibos, sin una llamada de redacción.

Los fallos se clasifican antes del worker: aserciones concretas del runner son defectos de
implementación; errores/ausencias/timeouts del entorno quedan pendientes; capacidades no
soportadas bloquean; ambigüedades, decisiones y cambios de alcance requieren resolución humana.
Una propuesta arquitectónica del worker se conserva y detiene la unidad; no llama al arquitecto.

Un defecto dentro del alcance crea una **unidad de remediación nueva**, con objetivo,
criterios aprobados, evidencia del fallo y dependencias aceptadas. No reabre ni modifica las
slices anteriores. Reutiliza el motor de ejecución, sus controles arquitectónicos, cuota y
límites de intentos. Limita las escrituras a archivos modificados por las slices aceptadas de ese milestone;
si esa superficie no puede acreditarse o hace falta ampliarla, requiere una decisión.
Verifica las regresiones de las slices del milestone y sus gates
mecánicos; acepta código antes de repetir el cierre completo sobre el nuevo commit.

Cada problema identificado por milestone/gate/check tiene como máximo un ciclo automático
de remediación y revalidación, además de los límites internos del worker. El contador se
reserva antes del despacho y persiste independientemente de commits, textos, procesos, runs
y revisión del plan. Una repetición bloquea con diagnóstico y opciones; nunca abre intentos
ilimitados. Las revisiones humanas se solicitan de nuevo si cambia el candidato.

## Preparación y presupuestos persistentes

Tras cerrar, Factory escoge en orden de roadmap el primer milestone sin recibo vigente cuyas
dependencias estén cerradas. Continúa su trabajo preparado cuando sigue vigente. Si la próxima
slice está en outline, el refinador existente recibe solo objetivo/alcance, requisitos,
contratos/ADRs pertinentes, recibos previos, riesgos, código localizado, harness y gates.
Desarrolla esa slice dentro del milestone, sin reconstruir discovery, arquitectura, roadmap,
IDs, dependencias o trabajo aceptado. Si el outline carece de criterios, solo puede heredar
comprobaciones `after_slice` ya aprobadas y vinculadas a oráculos autorizados; si faltan, bloquea.

El overlay de preparación pasa los gates de referencias, cobertura, alcance y arquitectura.
Hay como máximo una propuesta y una corrección por preparación de milestone/fuentes, incluso
si cambia el código o se reinicia. Las propuestas y preguntas quedan guardadas. Se mantiene
el máximo de tres slices detalladas sin aceptar y solo se concreta la próxima candidata.

El run conserva deadline, llamadas, tokens observados e intentos entre milestones. Incluye
preparación, refinamiento, implementación y remediación, desglosados en el estado. Una
remediación aceptada cuenta como una unidad de `max_slices`: con límite tres, dos slices y
una remediación consumen las tres, aunque haya un segundo milestone elegible. Una validación
local puede ejecutarse sin llamadas disponibles/cuota de modelo, respetando pausa, tiempo,
tokens y recursos restantes. Antes de cada llamada al modelo se comprueba cuota fresca y su
reserva. No se baja el 25 %, cambia proveedor/bucket ni usa facturación alternativa para el smoke.

`factory_status.continuation` muestra milestone, slice/gate activo, siguiente milestone,
recibos cerrados, problemas abiertos, causa de parada y presupuesto restante.
`factory_inspect milestones` añade recibos e historia; `verification`, resultados y criterios;
`requirements`, aportaciones y aceptación completa; `refinement`, inputs y overlays.
La skill no supervisa el loop ni mantiene la conversación abierta para sostenerlo.

Resultados y único piloto preparado: [continuation-validation.md](continuation-validation.md).
