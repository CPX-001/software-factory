# Segundo piloto real: entrega verificada con vinculación automática

**El mismo piloto alcanzó `project_verified` y tiene entrega local.** El planner real produjo
la vinculación, su crítico la revisó y Factory la compiló. Después implementó dos slices
dependientes, cerró ambos milestones y validó el producto integrado desde una copia limpia.
La consulta humana del informe en otro chat de Codex App sigue pendiente; no se declara
completada esa parte de la aceptación end-to-end.

La [evidencia final](evidence/end-to-end-12-auto-real-delivery.json), el
[plan aceptado](evidence/end-to-end-12-auto-accepted-plan.json) y la
[definición compilada](evidence/end-to-end-12-auto-verification-binding.json) conservan el
resultado. Los bloqueos a [10 llamadas](evidence/end-to-end-12-auto-real.json) y
[23 llamadas](evidence/end-to-end-12-auto-recovery-real.json) permanecen como historia.
La primera [entrega real](end-to-end-acceptance.md) conserva su commit, recibo y consumo.

## Instancia y resultado

- Proyecto: `p_ba973102351c9eac`, Records end-to-end pilot.
- Producto: `/home/cpx/.local/share/software-factory/pilots/records-auto-v1/product`.
- Run: `50fe4228-5bee-4f71-a660-dcc6357768db`.
- Continuation: `2abc197c-eac7-497a-850e-d4c5379cf9cf`.
- Commit validado en `factory/accepted`: `74b9861896a571cf3d9c271dcf9a0e6fd2b32abb`.
- Recibo: `sha256:b9b0685f90250bfd830f150d99607e8f125f333efa1095f9a34104fb2f1aeca1`.
- Entrega: `product/.factory/deliveries/<recibo>/REPORT.md`, con código en `source/`.

La rama inicial `master` sigue en `3f84d77a3f327c37b02cc2235e2465e2744a87a9`, que añade
únicamente el oráculo autorizado antes de implementar. El seed original es
`ce2299d5292d32f0511890c400ef4ebe014141fa`. No hubo cambio de rama del usuario ni cambios
en el producto después de su aceptación; no hay remotos Git, push, publicación ni despliegue.

## Recorrido real y consumo

| Fase | Llamadas contabilizadas | Tokens observados | Resultado |
| --- | ---: | ---: | --- |
| Discovery | 1 | 9.370 | Completado |
| Arquitectura | 2 | 23.585 | Baseline aceptada |
| Planning | 24 | 642.826 | Roadmap y vinculación aceptados |
| Implementación | 2 | 39.478 | Dos slices aceptadas al primer intento |
| Refinamiento, preparación y remediación | 0 | 0 | No necesarios |
| Validación final y entrega | 0 | 0 | Checks deterministas y recibo |
| Total | **29** | **715.259** | Sin uso pendiente de clasificación |

Una llamada fue rechazada antes de generación por HTTP 400 `invalid_json_schema`.
Sigue contabilizada y conserva el uso SDK ausente; no se fabrica una medición de cero tokens.
Modelo/esfuerzo: `gpt-5.6-terra/low`, autenticación ChatGPT, SDK/runtime 0.147.0.
Fast se solicitó como `service_tier=priority`, ofrecido por el modelo. No hay notificación
del nivel aplicado ni medición de aceleración.

La autorización inicial fue 20 llamadas, 30 minutos y 500.000 tokens. Aplicando la instrucción
posterior de ampliar lo necesario, cinco ampliaciones registradas llevaron el límite a
30 llamadas, 850.000 tokens y 14.400 segundos desde el inicio original. La última ampliación
de tiempo cubrió la espera de una respuesta humana. No se reiniciaron contadores: el deadline
quedó en 2026-09-11 18:15:47 UTC. Reserva 0 % por autorización explícita anterior; agotamiento
real y uso desconocido siguen bloqueando. No cambió proveedor, cuenta ni facturación.

## Decisión humana, correcciones e intervenciones

Hubo nueve concesiones explícitas de recuperación sobre propuestas distintas. Se registran
como intervención operativa; este recorrido no demuestra ausencia de intervenciones durante
el desarrollo de Factory. Ninguna intervención implementó el producto, escribió su mapping
fuera del planner, editó SQLite o marcó manualmente una fase como completada.

La decisión humana 1 pidió evidencia para la ausencia de almacenamiento residual. La respuesta
real «Sí, incorpora la prueba y continúa» autorizó
[test_no_residual_storage.py](../pilots/records-v1/test_no_residual_storage.py).
La [autorización registrada](evidence/end-to-end-12-auto-storage-authorization.json) conserva
la respuesta, el commit del recurso, la nueva definición y la reanudación del mismo run.
Los seis procedimientos, 16 recursos, alcance y entrega originales quedaron intactos.
El recurso añadido está fuera de los permisos de escritura del worker.

Las correcciones de Factory reutilizan sus mecanismos existentes:

- Esquema compatible y clasificación precisa del rechazo previo a inferencia.
- Propietarios de riesgos y hallazgos vinculados a referencias existentes.
- Recuperación autorizada con historial, presupuesto agregado y protección de pausa/proceso.
- Recuperación de respuestas terminadas sin repetir inferencia ni consumo.
- Gate calculado sobre la propuesta actual; contexto fiel de cierres y aislamiento.
- Checks finales para requisitos de milestones anteriores, conservando los cierres exigidos.
- Diagnósticos con IDs, índices y textos de criterios todavía sin comprobación.
- Evidencia adicional inmutable antes de aceptar el plan, sin responder por el usuario.
- Referencias del crítico a códigos de diagnóstico de su contexto guardado: se conservan sus
  hallazgos y se rechazan códigos ausentes. Esta última corrección recuperó la revisión ya
  guardada con cero llamadas; el planner corrigió después la condición de cierre sin debilitarla.

Los detalles y regresiones están en [recuperación de planning](planning-recovery.md).
Después de aceptar el binding, todas las transiciones hasta la entrega ocurrieron sin otra
intervención. La remediación final automática estaba desactivada y no se utilizó. Cuando se
autoriza por separado, conserva su único ciclo global persistente y el presupuesto agregado.

## Aceptación independiente y entrega

Pasaron **26 tests independientes**, en siete procedimientos. Los 25 originales se conservaron;
el nuevo test ejecuta seis ejemplos CLI y detecta escrituras, SQLite y otros procesos mediante
eventos de auditoría, incluidos intentos cuyo error captura el producto. Su garantía se limita
a esos recorridos Python, no a código nativo arbitrario ni todos los inputs posibles.
Las integraciones usan los módulos reales; no hay servicios externos simulados.

El recibo final acredita 25 criterios con 18 bindings de siete procedimientos distintos.
Once bindings reutilizan resultados idénticos sobre el mismo código, runner y entorno.
Todos corresponden al commit final; los recibos históricos de milestones no sustituyen la
comprobación del producto integrado. Los cambios relevantes de commit, fuentes, definición,
entorno o decisiones invalidan la evidencia aplicable y quedan fuera de esa aceptación.

Se reutilizan `Verifier`, la exportación de blobs Git y `LinuxSandbox`: Python de sistema
con `-I -S`, biblioteca estándar declarada, red deshabilitada, producto de solo lectura y
entorno temporal vacío. No hereda `.venv`, secretos, bases residuales, archivos sin versionar
ni estado interno de Factory. El informe queda fuera del código validado.

Desde el `source/` de la entrega se comprobó también el comando documentado:

```bash
/usr/bin/python3 -S category_report.py examples/valid.json
```

Salida exacta:

```json
[{"category":"food","count":2,"total":12},{"category":"books","count":1,"total":7}]
```

El informe contiene los comandos de los oráculos, recibo, contrato, runner y ubicación del
código. Los archivos exportados coinciden con sus hashes validados; no incluyen credenciales,
bases internas ni transcripts. Garantiza las condiciones registradas para esa versión y
entorno. No acredita seguridad universal, otros stacks, despliegue ni proyectos grandes.

## MCP, App y reproducción

La [consulta nativa y petición repetida](evidence/end-to-end-12-auto-delivery-native.json)
conservan el mismo run, commit, único recibo y consumo 29/715.259. El cliente MCP instalado
se desconectó después de la respuesta humana; el controller terminó autónomamente.
El mismo smoke consultó el PASS independiente y reconectó sin crear otra instancia.

Las dos pruebas humanas anteriores de selección/envío pausado en chats distintos siguen
registradas y no necesitan repetirse. La consulta humana de la entrega final sigue pendiente.
Las tools nativas y el cliente MCP real no sustituyen esa observación de la UI.
El launcher instalado carga el código actual del repositorio; una conexión nativa antigua
puede conservar módulos/esquemas previos hasta abrir otra conversación.

Consulta comprobada, sin inferencia ni nuevo run:

```bash
.venv/bin/python scripts/smoke_continuation.py \
  --prepared /home/cpx/.local/share/software-factory/pilots/records-auto-v1/report.json --installed
```

Recrear usa `--from-discovery --automatic-binding --directory /ruta/nueva` según
[la preparación existente](planning-binding.md); rechaza sobrescribir una instancia y ya
incluye los 26 tests antes de discovery. Reanudar usa el mismo `--prepared` con `--run`:
conserva historial/límites y reconoce el resultado validado. No se ha creado un tercer piloto.

La suite completa de `9ff7630` pasa **342 tests en 694.339 segundos**, sin fallos, errores,
omisiones ni inferencia. El [log](evidence/end-to-end-12-auto-delivery-tests.txt) y el
[manifiesto de 91 fuentes](evidence/end-to-end-12-auto-delivery-checks.json) identifican
la versión comprobada. Los modelos de la suite son simulados; Git, Python y aislamiento
son reales. La evidencia con modelo real es la del piloto.
