# Validación final y entrega local (paso 11)

`project_verified` significa que las condiciones aprobadas del proyecto han pasado en un
commit concreto de `factory/accepted`, con sus fuentes y entorno registrados. No significa
despliegue, calidad universal ni ausencia de vulnerabilidades. El workflow conserva la fase
`execution`; el estado y el recibo de proyecto expresan esta aceptación específica.

## Contrato previo y condiciones

`ProjectGate` especializa `MilestoneGate` dentro de `Continuation`. Reutiliza preparación,
verificación, decisiones, clasificación de fallos, remediación mediante `Execution`, bloqueos,
presupuestos, recuperación y publicación cercada. No hay otro controller ni revisión LLM final.

La definición de ejecución puede incorporar `project_acceptance`:

```json
{
  "entry_checks": ["main_entry"],
  "delivery_paths": ["USAGE.md"],
  "runtime": "python_stdlib",
  "exclusions": []
}
```

Es una parte de `verification`, no una definición independiente de éxito. `entry_checks`
referencia checks existentes de API pública (`python_behavior`) o de `python_unittest`
con una entrada real declarada. Para una CLI, el check añade:

```json
{
  "entrypoint": {"path": "app.py", "args": ["-1", "2"], "stdout": "sum=2\n"}
}
```

El runner ejecuta el archivo en otro proceso Python después de sus tests, y compara salida
y código de salida. No acepta comandos shell. Los casos, paths y salidas deben ser parte de
la aceptación revisada antes de implementar; Factory no los redacta al cerrar.

Cada requisito `covered` necesita su contrato completo `requirement_acceptance` ya existente:
`requirement`, `condition`, todos sus `milestones` contribuyentes y `checks`. Incluye éxito,
restricciones y contratos transversales. Sumar contribuciones parciales nunca satisface el
requisito. Se ejecutan los gates no locales comprometidos, sus condiciones de milestone y
los checks seleccionados de aceptación completa/entrada real. Los gates `system` pueden
usar `project_close` para obligaciones que solo se ejecutan tras cerrar todos los milestones;
`project_checkpoint` conserva su ejecución estratégica anterior y se comprueba de nuevo al final.

Todos los milestones deben tener recibos vigentes para las fuentes/definición. Faltas de
mapeo, documentos exigidos, capacidad, dependencias, revisiones o decisiones bloquean. Los
criterios subjetivos mantienen sus índices aprobados y necesitan `human_review` y respuesta
`accept` sobre el candidato concreto. Cero tests, skips, expected failures, errores de entorno,
timeouts o ausencia del marcador final del runner no cuentan como PASS.

Una cobertura `deferred` o `out_of_scope` conserva su justificación y debe referenciar en
`exclusions` una decisión previa mediante `requirement`/`decision_id`. Esa decisión debe
identificar la clave y la disposición, y tener respuesta humana `accept` antes de congelar
el contrato. El contrato conserva la decisión completa; no toma una autorización posterior
para convertir retrospectivamente un hueco en exclusión. Tras aceptar trabajo, los criterios
y exclusiones de ese contrato no pueden cambiar bajo la misma revisión de planning.

Los checks de integración declaran `integration_mode`: `local`, `simulated` o
`external_service`. Una integración explícita sin clasificación deja un hueco. `simulated`
aparece como tal en evidencia e informe; `external_service` bloquea en el entorno offline.
La suficiencia semántica de los oráculos sigue dependiendo de la aceptación previamente
revisada; el gate no demuestra cualquier frase en lenguaje natural mediante análisis universal.

## Versión, aislamiento y evidencia

El contrato se guarda por fingerprint en `project_contracts`, dentro del esquema SQLite v9.
Referencia la baseline arquitectónica, la revisión de planning, los requisitos completos,
la definición inmutable y las decisiones de exclusión. Una migración no habilita permisos.

La validación fija `factory/accepted`. Su identidad incorpora contrato, fuentes, decisiones
pertinentes, recibos de milestone, ADRs, autorización, commit, entorno y hashes de Python,
stdlib y herramientas del verificador. El export lee directamente los blobs regulares de
Git: no ejecuta filtros ni aplica reglas `export-ignore`, no copia el worktree ni `.git`.
Rechaza symlinks, submódulos, `.venv`, bytecode y material protegido. Comprueba de nuevo la
identidad, archivos y directorios extra antes de publicar. Los documentos del producto
deben existir en ese commit; el informe posterior no modifica el producto.

Los checks reutilizan `LinuxSandbox` con namespaces de usuario/mount/PID/red y seccomp,
código de solo lectura, home/tmp vacíos y entorno por allowlist. El perfil limpio monta
Python del sistema, su stdlib y una lista explícita de librerías nativas, con rutas y hashes;
no monta directorios completos de paquetes nativos. Excluye `.venv`, paquetes de sitio,
`/usr/local` y CLIs del host. Python se ejecuta con `-I -S`; la CLI hija usa `-S` con el mismo
entorno limpio. No se heredan credenciales ni se habilita red o instalación. Una dependencia
no disponible queda como bloqueo de entorno y no dispara reparación de producto.

Las verificaciones históricas de milestones no prueban el commit integrado final. Se
reutiliza un resultado guardado solo con la misma identidad completa y definición de check.
Un cambio de código, requisitos, configuración relevante, entorno o decisión invalida esa
reutilización. Por ahora la invalidación por commit es conservadora: no calcula un grafo
de archivos afectados. Los resultados se guardan por check mediante archivo atómico durable
y journal, permitiendo recuperar el intervalo entre la ejecución y la escritura SQLite.

La publicación comparte la transacción Git `verify/prepare` sobre la referencia aceptada
y el writer SQLite con `MilestoneGate`. Recomprueba fuentes, candidato, pausa, propietario
y presupuestos. El recibo y el evento único `project_verified` se confirman juntos. Cambios
posteriores conservan ese recibo histórico y aparecen como otra versión pendiente.

## Autorización, continuidad y remediación

Para cerrar automáticamente el recorrido ya autorizado de milestones, añadir a la política:

```json
"final_validation": {"enabled": true, "automatic_remediation": false}
```

Se mantiene el consentimiento independiente para `continuation.enabled`, el salto entre
milestones y los límites. Omitir `final_validation` conserva el límite anterior. Sobre un
proyecto ya listo, «Valida el proyecto terminado y prepara su entrega local» se transmite a
`factory_execute` con `action=validate_project`, ID de proyecto y `request_id` estable.
`automatic_remediation=true` necesita autorización separada; también puede autorizarse
después de observar un fallo, conservando su evidencia y todos los contadores.

La validación determinista no consulta cuota ni crea un modelo. Una corrección reutiliza
`Execution`, parte del código aceptado y escribe solo en archivos identificados por las
aceptaciones previas y permitidos por la política. Conserva oráculos y criterios, ejecuta
regresiones y gates, añade un commit y repite la validación final. Nunca reabre recibos de
milestones. Cambios de arquitectura, alcance o dependencia relevante bloquean/proponen;
no se resuelven automáticamente.

`project_remediation` reserva como máximo **un ciclo automático global por proyecto**.
Es inmutable y sobrevive a nuevos commits, fallos, runs, revisiones y reinicios. Además
consume intentos, llamadas, tokens, tiempo y unidades del run original. `resume` y la entrada
en validación no renuevan nada. Un deadline agotado sigue siendo un bloqueo; no hay una
ampliación implícita del presupuesto. Si la reserva de cuota impide la corrección, se
conserva el fallo final y se bloquea exclusivamente esa inferencia.

## Entrega y consultas

La entrega queda en `.factory/deliveries/<receipt-id>/REPORT.md`, fuera del código validado.
Incluye `source/` exportado del commit, `receipt.json`, `contract.json`, checks y el runner
versionado que los ejecutó. No copia SQLite, transcripts ni directorios de runtime. Es una
proyección local, sin cambiar la rama del usuario, push, paquete publicado o despliegue.

El informe determinista contiene alcance, versión, procedimientos ejecutados, evidencias,
limitaciones y exclusiones. Los comandos repiten la entrada real y los mismos oráculos,
con rutas relativas a la entrega y el mismo directorio de trabajo del producto, también para
fixtures versionadas. La identidad del sistema y del aislamiento original está
en el recibo; ejecutarlos en otro host no afirma que ese host tenga el mismo aislamiento.

`factory_status.project_validation` separa candidato, versión histórica/actual, checks,
criterios pendientes, corrección, bloqueos, exclusiones y ubicación del informe.
`factory_inspect` con `view=project_validation` muestra todo el journal y los recibos.
`delivery_pending` conserva la aceptación pero necesita recuperar el informe. MCP puede
desconectarse: el proceso autorizado sigue trabajando fuera de la conversación.

También están disponibles `validate-project --request-id ID [--automatic-remediation]`
y `project-validation-show` en `python -m factory`, además de pausa/resume y las vistas
anteriores. Las solicitudes repetidas no reabren discovery, arquitectura ni planning.

Resultados y validaciones pendientes: [evidencia del paso 11](project-validation-results.md).
Mejoras justificadas por la revisión: [trabajo futuro](future-improvements.md).
