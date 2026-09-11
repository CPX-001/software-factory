# Vincular planning a las pruebas autorizadas

`policy.automatic_plan_binding=true` permite que el controller continúe desde planning
hasta ejecución sin una intervención para crear el mapping. Es una autorización opcional
previa a planning; no amplía políticas ya guardadas. Usa el mismo planner, su revisión,
`Execution.configure`, controller, verificadores y ledger. No añade una fase de inferencia.

La política inicial sigue declarando modelo/esfuerzo, rutas y límites finitos para todas
las fases. `continuation.inter_milestone` y `final_validation`, incluida su remediación,
conservan sus autorizaciones separadas. Una vez autorizados, un mensaje inicia el recorrido;
si el proyecto estaba pausado, necesita la reanudación explícita de esa pausa.

## Contrato y protección

Antes de inferencia se fijan las fuentes de los unittest y los recursos de verificación
declarados, todos versionados en Git y fuera de los permisos de escritura del worker.
`verification.resources` contiene sus rutas relativas y hashes. Los archivos auxiliares
deben declararse: no se descubren ni instalan dependencias arbitrarias. El perfil sigue
siendo Python/unittest de biblioteca estándar, sin red. El contexto de revisión es acotado;
si los recursos no caben, bloquea sin truncarlos.

`plan.execution_binding` referencia la definición autorizada, las plantillas y sus gates,
criterios, harness y requisitos. Su esquema no admite nuevos casos, comandos, mínimos,
timeouts ni código de tests. Conserva cada procedimiento original y su ID; puede repetirlo
con otro ID para otro gate. Los textos de aceptación completa se copian literalmente de
los requisitos de discovery. Un requisito transversal exige referencias a todos los
milestones contribuyentes y una comprobación final de aceptación completa.

El gate de planning reutiliza los comprobadores de cobertura y capacidades. Su crítico
habitual es obligatorio para este modo y recibe las fuentes de los oráculos: debe rechazar
un mapping que cubre índices pero no acredita el comportamiento declarado. Esto no es una
prueba formal de adecuación semántica de cualquier unittest. Si los oráculos no bastan para
los criterios originales, queda un hueco de aceptación; el sistema no escribe otros más fáciles.

Las exclusiones solo referencian `scope_authorizations` previamente declaradas con su
disposición y cita exacta de un mensaje de discovery, o una decisión humana real registrada.
La cita debe existir en un turno completado anterior al plan. El modelo no aporta un
`accept` ni reclasifica requisitos al cerrar. Contrato, revisión y referencias quedan
versionados en las revisiones de planning y definiciones de ejecución existentes.

La publicación comprueba propietario del proceso, pausa, revisión aprobada y fingerprint
del contrato. Conserva run, llamadas, tokens y deadline. Una caída antes de publicar
recupera la propuesta/revisión guardada; después de publicar recupera el mismo contrato.
Un cambio en un recurso fijado bloquea; restaurarlo permite continuar sin repetir análisis
vigente. Las peticiones de discovery ya completadas se reconocen por ID incluso después
de cambiar de fase, sin convertir su repetición en una respuesta humana posterior.

## Preparación del mismo escenario

El smoke existente acepta una preparación opcional, sin inferencia ni política aplicada:

```bash
.venv/bin/python scripts/smoke_continuation.py --from-discovery --automatic-binding \
  --directory /ruta/nueva/records-v1 --model gpt-5.6-terra --effort low
```

Prepara los mismos 25 tests independientes usados en el piloto real, ahora todos presentes
antes de discovery, y propone `verification_templates` en `report.json`. No precarga un
plan ni una arquitectura. La política propuesta debe autorizarse mediante la tool existente
con límites adecuados; la preparación conserva límites pequeños y la reserva por defecto.
La opción no habilita inferencia, no modifica instancias anteriores y rechaza sobrescrituras.
Una instancia existente se consulta/reanuda con `--prepared`, sin añadir opciones de creación.

La política admite `service_tier="priority"` cuando el usuario pide Fast. El adaptador
comprueba el catálogo del modelo antes de inferencia y transmite el nivel a threads nuevos,
reanudados y a cada turno. Conserva modelo, esfuerzo, cuenta y límites; no cambia la
configuración global ni habilita facturación API. Un nivel no ofrecido bloquea. La
telemetría distingue el nivel solicitado del observado cuando llega esa notificación.
Seleccionar Fast no demuestra una aceleración medida del recorrido completo.

## Evidencia y límite de esta corrección

`tests/test_planning_binding.py` reutiliza el producto de dos milestones en repositorios
desechables. Recorre discovery, arquitectura, planning, tres slices, cierre y entrega con
modelos simulados; Git, Python, los checks limpios y el transporte MCP son reales. Comprueba
recuperación a ambos lados de la publicación, pausa, procesos obsoletos, cambios de recursos,
huecos transversales, capacidades ausentes, exclusiones previas y repetición tras reconexión.
El controller se ejecuta después de desconectar el cliente de prueba. La independencia
del proceso separado respecto al host MCP también conserva sus tests existentes.

El piloto real anterior conserva sus 17 llamadas, commit y recibo. No se atribuye a ese
recorrido esta vinculación automática: allí hubo una intervención revisada. Probar el
modo nuevo con modelo real necesita autorización para superar el máximo de un piloto real
del encargo. La comprobación humana del informe final en otro chat también sigue pendiente.

La suite completa pasa **321 tests en 533,917 segundos**. La [evidencia de esta corrección](evidence/end-to-end-12-automatic-binding.json)
incluye hashes de fuentes, log, versión instalada del plugin y la consulta al piloto sin
cambio de commit, recibo o consumo. No hubo inferencia real adicional.
