# Segundo piloto real: decisión de evidencia pendiente

El segundo piloto sigue en **planning, esperando la decisión humana 1**. No tiene código
implementado, aceptación independiente ni entrega. El goal sigue activo; no se declara
completada la aceptación end-to-end. La primera [entrega real](end-to-end-acceptance.md)
conserva su commit, recibo y consumo originales.

La [evidencia actual](evidence/end-to-end-12-auto-recovery-real.json) conserva las llamadas,
recuperaciones, extensiones y estado. El [bloqueo anterior](evidence/end-to-end-12-auto-real.json)
a las diez llamadas permanece como historia, no como descripción del estado actual.

## Instancia, autorización y consumo

- Proyecto: `p_ba973102351c9eac`, Records end-to-end pilot.
- Producto: `/home/cpx/.local/share/software-factory/pilots/records-auto-v1/product`.
- Run: `50fe4228-5bee-4f71-a660-dcc6357768db`.
- Continuation: `2abc197c-eac7-497a-850e-d4c5379cf9cf`.
- Modelo/esfuerzo: `gpt-5.6-terra/low`, autenticación ChatGPT, SDK/runtime 0.147.0.

| Fase | Llamadas contabilizadas | Tokens observados | Resultado |
| --- | ---: | ---: | --- |
| Discovery | 1 | 9.370 | Completado |
| Arquitectura | 2 | 23.585 | Baseline aceptada |
| Planning | 20 | 524.877 | Espera decisión de evidencia |
| Implementación y validación final | 0 | 0 | No iniciadas |
| Total | **23** | **557.832** | Sin uso pendiente de clasificación |

Una llamada fue rechazada antes de generación por HTTP 400 `invalid_json_schema`.
Sigue contabilizada y conserva el uso SDK ausente; no se fabrica una medición de cero tokens.

La autorización inicial fue 20 llamadas, 30 minutos y 500.000 tokens. Aplicando la instrucción
posterior de ampliar lo necesario y corregir los obstáculos, se registraron ampliaciones
explícitas: 7.200 segundos desde el inicio original, después 24/650.000 y 28/750.000
llamadas/tokens. No se reiniciaron contadores. El deadline actual es 2026-09-11 16:15:47 UTC.
La reserva 0 % procede de la autorización explícita anterior. Agotamiento real y consumo
no conocido siguen bloqueando; no cambió proveedor, cuenta ni facturación.

Fast se solicitó como `service_tier=priority`, ofrecido por el modelo. No hay notificación
del nivel aplicado ni medición de aceleración. La parada actual es una decisión de aceptación,
no un bloqueo de cuota.

## Correcciones e intervenciones

Se mantuvieron el mismo run, arquitectura y propuesta en curso. Ninguna intervención
implementó el producto ni escribió su mapping fuera del planner. Hubo siete concesiones
explícitas de recuperación de propuestas distintas; esto sigue siendo una intervención
operativa observada y no una demostración de cero intervenciones.

Las correcciones de Factory reutilizan sus mecanismos existentes:

- Esquema de salida compatible; clasificación precisa del rechazo previo a inferencia.
- Propietarios de riesgos vinculados a milestones/slices y sus referencias inversas.
- Recuperación autorizada con historial, presupuesto agregado y protección de pausa/proceso.
- Hallazgos que referencian bindings válidos y recuperación del checkpoint de un crítico ya
  terminado, sin repetir su inferencia ni consumo.
- Gate calculado sobre la propuesta actual, sin enviar al crítico errores de la anterior.
- Contexto fiel de recibos de cierre y aislamiento, con sus garantías y límites.
- Checks finales que revalidan requisitos de milestones anteriores sin añadir contribuyentes
  ficticios; conservación de los recibos exigidos.
- Diagnósticos con los IDs originales y los criterios concretos sin correspondencia.

Los detalles y regresiones están en [recuperación de planning](planning-recovery.md).
Los oráculos detectaron carencias de cobertura reales; los gates no se relajaron para aprobarlas.

## Decisión 1 y solución preparada

El planner mantiene bloqueado `runtime_constraint` porque los oráculos iniciales no
establecen explícitamente ausencia de almacenamiento residual. La copia limpia prueba
independencia de estado previo, pero permite temporales efímeros y no es una auditoría
universal de efectos secundarios.

Se preparó [test_no_residual_storage.py](../pilots/records-v1/test_no_residual_storage.py):
usa el runner Python existente sobre los seis ejemplos CLI originales y detecta escrituras,
SQLite y creación de otros procesos mediante eventos de auditoría. Detecta también intentos
cuyo error captura el producto. La regresión usa productos desechables; cubre una utilidad
sin estado, un archivo residual, un temporal y SQLite. Su garantía se limita a esos caminos
Python, no a código nativo arbitrario ni todos los inputs posibles.

La definición concreta está preparada en
`/home/cpx/.local/share/software-factory/pilots/records-auto-v1/proposed-storage-evidence-extension.json`.
Conserva los seis procedimientos/25 tests originales y añade un check independiente.
**Todavía no se ha aplicado al piloto real ni se ha respondido por el usuario.**

`planning_recovery.verification_extension=true` permite registrar esa evidencia adicional
antes de aceptar el plan, conservando inmutables la definición anterior y todas sus
condiciones. La prueba está fuera de permisos de escritura del worker. La decisión pendiente
impide inferencia; al registrar una respuesta real, continúa el mismo controller y revisa
la nueva vinculación. No se cambian arquitectura, alcance o exclusiones para lograr PASS.

Las preparaciones futuras del mismo escenario incluyen este check antes de discovery:
26 tests en total. No se ha creado ni autorizado un tercer piloto real.

## MCP, App y reproducción

La tool nativa inició esta instancia y ha consultado su progreso y decisión pendiente.
El proceso autónomo sobrevivió a las desconexiones MCP. Las recuperaciones usaron procesos
nuevos del launcher instalado; el servidor nativo ya abierto conserva código anterior para
algunos contadores y esquemas. El plugin actualizado es
`0.1.0+codex.20260911155308`. Esto no equivale a una observación humana del informe en la UI.
Las dos pruebas humanas anteriores de selección/envío pausado en chats distintos siguen
registradas y no necesitan repetirse. La consulta humana de una entrega final sigue pendiente.

Consulta comprobada, sin inferencia ni nuevo run:

```bash
.venv/bin/python scripts/smoke_continuation.py \
  --prepared /home/cpx/.local/share/software-factory/pilots/records-auto-v1/report.json --installed
```

Recrear usa `--from-discovery --automatic-binding --directory /ruta/nueva` según
[la preparación existente](planning-binding.md); rechaza sobrescribir una instancia.
Reanudar usa el mismo `--prepared` con `--run` y conserva historial/límites. Una reanudación
no responde decisiones ni amplía presupuestos. Para el piloto actual falta la decisión 1;
no hace falta volver a iniciar discovery, arquitectura ni cada fase posterior.

La suite completa del código `54db76a` pasa **341 tests en 553.021 segundos**,
sin fallos, errores ni omisiones y sin inferencia. El [log](evidence/end-to-end-12-auto-recovery-tests.txt) y el
[manifiesto de 91 fuentes](evidence/end-to-end-12-auto-recovery-checks.json) permiten
comprobar esa versión. Los modelos de la suite son simulados; Git, Python y aislamiento
son reales. La aceptación con modelo real continúa pendiente de la decisión y de ejecutar el producto.
No hay push, publicación, despliegue, otros lenguajes ni cambios arquitectónicos automáticos.
