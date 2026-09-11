# Validación del paso 10

## Modelos simulados y verificaciones reales

La suite habitual no consume cuota. Usa modelos simulados para las propuestas y la
implementación, repositorios temporales, commits reales y Python/unittest en el aislamiento
normal de Factory. Los valores de tokens de FakeSDK son sintéticos.

El escenario `test_milestone` demuestra dos milestones dependientes:

1. A implementa suma; B compone el resultado y introduce una regresión de suma con negativos.
   Sus checks locales pasan, pero `test_integrated.py` falla con una aserción real al comprobar
   la composición sobre todo el código aceptado.
2. Factory conserva las dos slices aceptadas y abre una unidad de remediación con ese fallo.
   La corrección pasa las regresiones y checks estratégicos, publica código aceptado y repite
   el gate integrado en el nuevo commit. Se publica un recibo de cierre de m1.
3. m2 parte de outline. El refinador recibe código, recibo, cobertura y gates aceptados de m1;
   prepara únicamente su próxima slice. Su implementación empieza sin otro mensaje ni otro
   `factory_execute`, y Factory termina con ambos recibos y `project_ready_for_validation`.

Hay cobertura adicional de requisito transversal activo tras m1; aceptación completa explícita;
criterios subjetivos y respuesta humana sobre commit exacto; definición ausente, tests ausentes
y capacidades no soportadas; checkpoint de sistema soportado; protección de criterios/oráculos;
evidencia obsoleta; remediación agotada, incluso observando el mismo problema en otro commit;
recuperación tras verificar y tras cerrar; publicaciones de procesos sustituidos; recibos
idempotentes e históricos tras revisar fuentes; límite de tres unidades incluyendo remediación;
preparación con una sola corrección; pausa, duración, falta de llamadas disponibles y migración
de runs antiguos sin ampliar autorización ni reiniciar sus presupuestos.

La definición aditiva de criterios subjetivos conserva lectura de planes anteriores; los nuevos
outputs del planner deben declarar esos índices. Se ajustó la prueba del adaptador al nuevo
contrato explícito. Las fixtures antiguas sin definiciones de cierre terminan ahora en
`validation_pending`, conservando todas sus aceptaciones y sin inventar un PASS.

La suite completa pasó **261 tests en 533,315 s**, sin cuota, con
`.venv/bin/python -m unittest discover -v`. El [log completo](evidence/continuation-10-tests.txt)
conserva el resultado. También pasaron compileall, `git diff --check` y los validadores de
plugin/skill. La fixture del smoke se volvió a comprobar tras añadir sus casos fijos al
gate de cierre, sin inferencia.

## Integración MCP

`test_milestone_mcp` usa MCP stdio y un proceso de controller separado, con modelos simulados
y verificaciones reales. El cliente se desconecta antes de que A termine. El proceso completa
A → B → fallo integrado → remediación → cierre m1 → preparación m2 → primera slice y cierre m2.
Otra conexión inspecciona el resultado; solicitudes duplicadas conservan el mismo run y recibos.
Esta prueba verifica transporte y autonomía; no demuestra ejecución con modelo real ni uso de UI.

El plugin local se reinstaló con cachebuster `0.1.0+codex.20260911004932`. Una conexión nueva al
entrypoint instalado comprobó las diez tools, `continuation.inter_milestone`, los dos milestones
y su política persistente, sin iniciar ninguna ejecución.
[Evidencia de MCP instalado](evidence/continuation-10-installed-mcp.json).

## Modelo real: pendiente, reserva respetada

El 11 de septiembre de 2026 a las **00:44:33 UTC**, el diagnóstico del adaptador real,
`account/rateLimits/read`, observó para `gpt-5.6-terra` / `low`, autenticado por ChatGPT:

| Dato | Observación |
| --- | --- |
| SDK/App Server incluido | 0.147.0 |
| Medidor aplicable | codex |
| Cuota usada / libre | 80 % / 20 % |
| Reserva | 25 % |
| Resultado | quota_reserve |
| Reset comunicado | 15 de septiembre de 2026, 09:00:30 UTC |
| Llamadas de inferencia del diagnóstico/smoke | 0 |

[Diagnóstico sanitizado](evidence/continuation-10-quota.json). No se inició un turno ni se
redujo la reserva, cambió proveedor/bucket o recurrió a facturación alternativa. La cuota es
compartida y no atribuimos sus cambios al desarrollo. El reset comunicado no garantiza cuota futura.

## Un único piloto preparado, reutilizando el pendiente

Se reutilizó **el mismo** proyecto `p_1f2f88a25a7f899c`, situado en
`/tmp/factory-continuation-smoke-bc7lwhrv/product`. Su preparación previa del paso 9 no había
iniciado ejecución. Se publicó una revisión sintética validada del plan para separar A y B
en m1 y m2; arquitectura, código de entrada y los catorce tests predeclarados permanecieron
intactos. Los gates de cierre ejecutan las suites sobre el código integrado. Esta adaptación
es exclusiva de la fixture desechable sin ejecutar; no es replanning entre milestones del motor.

La política mantiene dos unidades, cuatro llamadas, 300 segundos, 30.000 tokens observados,
180 segundos y 12.000 tokens por unidad, dos intentos individuales y reserva del 25 %; añade
`inter_milestone=true`. Si una remediación consume una de las dos unidades, el piloto se
parará por ese límite. No se aumenta para obtener un resultado favorable.

La preparación y autorización pasaron por MCP. **No se llamó a `factory_execute`**: no hay run,
deadline ni intento de inferencia consumidos. [Preparación](evidence/continuation-10-prepared.json).
Cuando la cuota lo permita, el mismo escenario se puede lanzar una sola vez:

```bash
.venv/bin/python scripts/smoke_continuation.py \
  --prepared /tmp/factory-continuation-smoke-bc7lwhrv/report.json --run
```

El driver comprueba cuota sin inferencia antes de iniciar el run; el worker vuelve a comprobarla
antes de cada llamada. Se niega a reemplazar fuentes o reiniciar un piloto ya intentado.

## Codex App UI: pendiente

La conexión Python MCP instalada **no es una prueba desde la UI**. En una conversación nueva,
para cargar la skill actualizada y usando el ID de ese proyecto con su política ya autorizada:

> Continúa el proyecto p_1f2f88a25a7f899c entre milestones dentro de la política autorizada y detente si necesitas una decisión.

No lanzar ese piloto mientras la cuota no respete la reserva. Continuidad y cierre con modelo
real, y la comprobación desde la UI de Codex App, siguen pendientes. Pasar tests adicionales
no las sustituye. Al cerrar el roadmap solo queda `project_ready_for_validation`: no se declara
el proyecto completed, listo para producción, publicado ni desplegado.

La evidencia anterior se conserva en los [tests del paso 9](evidence/continuation-9-tests.txt),
[su diagnóstico de cuota](evidence/continuation-9-quota.json) y
[la preparación original](evidence/continuation-9-prepared.json).
