# Paso 12: análisis con modelo real; aceptación integral pendiente

**El piloto ha completado discovery, arquitectura y planning con modelo real.** Todavía
no ha implementado el producto ni alcanzado `project_verified`. El bloqueo actual es
`verification_binding_pending`: los oráculos iniciales aún no están vinculados a todas
las condiciones del plan aceptado. La reserva de cuota dejó de bloquear este piloto
cuando el usuario autorizó expresamente retirarla.

La [evidencia del recorrido real](evidence/end-to-end-12-real-model.json) contiene versiones,
identificadores, llamadas, consumo, intervenciones y limitaciones. Los resultados históricos
con reserva del 25 % se conservan en [preflight](evidence/end-to-end-12-quota.json),
[piloto inicial](evidence/end-to-end-12-pilot.json) y [reconexión](evidence/end-to-end-12-reconnect.json).
No son el estado actual del piloto.

La suite completa sobre los últimos cambios pasa **304 tests en 444,215 segundos**, sin
fallos, errores ni skips. Se conservan el [log](evidence/end-to-end-12-real-model-tests.txt)
y el [manifiesto de fuentes](evidence/end-to-end-12-real-model-checks.json). También pasan
compilación y comprobación de whitespace. Esta suite no consume cuota LLM.

## Instancia y reproducción

El piloto anterior `p_1f2f88a25a7f899c` apuntaba a
`/tmp/factory-continuation-smoke-bc7lwhrv/product`; faltaban tanto ese repositorio como su
registro. La búsqueda de registros en `/home/cpx`, `/tmp` y `/var/tmp` no encontró una
instancia recuperable. Se reutilizó el escenario de registros para crear una única
instancia nueva persistente, sin copiar estado de otro host:

- Proyecto: `p_3084354cc76d1c23`, Records end-to-end pilot.
- Directorio: `/home/cpx/.local/share/software-factory/pilots/records-v1`.
- Producto: `product/`; registro: `/home/cpx/.local/state/software-factory/registry.sqlite3`.
- Commit de fixtures: `9af30b4ae36f421f6a41eb2b77011b9fbb303a65`; sigue siendo el HEAD del producto.
- Run: `9a2490b3-508b-4b60-bbb2-7aacf45d2901`.
- Continuation: `b5774d1c-550d-49da-89df-a40749fa6e97`.
- Estado: `execution` / `implementation_boundary`; no hay worker activo.

Para recrear la preparación **solo si el directorio no existe**:

```bash
.venv/bin/python scripts/smoke_continuation.py --from-discovery \
  --directory "$HOME/.local/share/software-factory/pilots/records-v1" \
  --model gpt-5.6-terra --effort low
```

La creación rechaza sobrescritura y rutas dentro de Factory. No carga arquitectura ni
planning ficticios. Sus fuentes son el [brief](../pilots/records-v1/brief.md), el
[fixture existente](../scripts/execution_smoke_fixture.py) y los
[tests de CLI](../pilots/records-v1/test_product_cli.py). `pilot-contract.json` fija sus hashes.
Las excepciones de presupuesto autorizadas para esta instancia no cambian los valores
predeterminados de una nueva preparación.

Para inspeccionar **esta misma instancia**, sin iniciar inferencia:

```bash
.venv/bin/python scripts/smoke_continuation.py \
  --prepared "$HOME/.local/share/software-factory/pilots/records-v1/report.json" --installed
```

`report.json` proyecta el último estado y `observations/` conserva cada observación. Añadir
`--run` consulta el preflight y solicita reanudar el trabajo autorizado mediante Factory.
La comprobación de identidad/contrato impide reconstruir el piloto como una reanudación.
En el estado actual, reanudar conserva la parada por falta de bindings; no vuelve a
hacer discovery, arquitectura o planning ni crea otro presupuesto.

## Alcance y aceptación independiente

Utilidad Python de biblioteca estándar para resumir y ordenar registros JSON. El helper
existente `record_rules.py` conserva `strip().casefold()`. Los oráculos independientes
preceden a la implementación: 10 tests del resumen, 4 del ranking y 5 del CLI, con entradas
válidas, vacías, desordenadas, negativas, bool, JSON malformado, Unicode, NaN/infinito,
no mutación y resultados exactos. El brief comunica estos criterios al workflow.

La entrada prevista es `python category_report.py examples/valid.json`. Aún falta
`category_report.py`: **este comando no se presenta como un producto entregado o ejecutado**.
También faltan `records.py` y `USAGE.md`. La aceptación independiente sigue en **NOT_RUN**;
no hay commits de implementación aceptados, recibos de milestones, recibo final ni entrega.

## Recorrido y consumo observados

| Fase | Llamadas reales | Tokens observados | Resultado |
| --- | ---: | ---: | --- |
| Discovery | 2 | 15.737 | 14 elementos de conocimiento; completado |
| Arquitectura | 4 | 48.022 | Baseline aceptada, revisión 1 |
| Planning | 7 | 120.882 | Roadmap aceptado, revisión 1: 2 milestones, 3 slices, 9 gates |
| Implementación y refinamiento | 0 | 0 | No iniciados |
| Total | **13** | **184.641** | Sin llamadas con consumo desconocido al terminar el análisis |

Modelo efectivo `gpt-5.6-terra`, esfuerzo `low`, proveedor OpenAI, autenticación ChatGPT,
SDK/runtime 0.147.0. Los workers usan el mismo adaptador aislado de ejecución, sin tools
recursivas de Factory. No se ha cambiado de proveedor ni usado facturación alternativa.
Los tokens del worker se distinguen de la cuota compartida de la cuenta.

El usuario autorizó reserva **0 %** y ampliar el presupuesto. Se registraron 20 llamadas,
1.800 segundos y 250.000 tokens agregados; 600 segundos y 50.000 tokens por llamada.
El límite inicial de unidades conserva 2; el roadmap real contiene 3 y esa diferencia debe
resolverse expresamente al vincular la ejecución. Se mantiene el mismo ledger para fases
iniciales y ejecución posterior. Las reanudaciones no reinician llamadas, tokens ni deadline.
Factory no ha consumido refills o reinicios de cuenta; el agotamiento real y la telemetría
no disponible siguen impidiendo inferencia.

## Intervenciones y correcciones

1. Se aplicó la autorización del usuario a las fases iniciales mediante el mismo
   `factory_execution_policy`, con los checks aún como plantillas. El controller reutiliza
   `CodexExecution`, su sandbox, watchdog y ledger de continuation; no hay otro supervisor.
2. Discovery volvió a pedir permiso porque no recibía la política efectiva. Se añadió la
   autorización al contexto y se reenvió la instrucción real del usuario con un request ID
   nuevo. Las decisiones de producto no se sustituyeron por respuestas inventadas.
3. Planning creó un ciclo entre cierre del milestone y cierre del proyecto. Se corrigió
   la instrucción contradictoria sobre `project_close`, se añadió rechazo determinista de
   esa dependencia y una reparación adicional acotada bajo el presupuesto global.
4. Una revisión objetó después una dependencia que el controller ya impone. Se documentaron
   esas garantías en el contexto y se volvió a revisar **la misma propuesta**, conservando
   todas las revisiones anteriores. Esa actualización de contexto puede solicitar una sola
   revisión adicional; no crea una nueva ronda de reconciliación ni supera ocho llamadas
   totales de planning. El análisis real terminó con siete.
5. El estado diferencia ahora el binding pendiente de una revisión de fuentes obsoleta.
   Vincular el plan no puede debilitar los oráculos iniciales ni renovar el presupuesto.

Estas intervenciones de desarrollo/reanudación quedan registradas: el recorrido real
**todavía no demuestra autonomía completa desde la idea hasta la entrega**.

## Bloqueo de integración conservado

El plan aceptado contiene nueve gates. Los tres oráculos predeclarados no constituyen por
sí solos un contrato completo que pruebe cada condición de esos gates. Quedan pendientes:

- Bindings concretos para criterios, integraciones y aceptación completa de requisitos
  transversales, conservando las fuentes y pruebas originales.
- Evidencia de que los comandos de `USAGE.md` se ejecutaron realmente y del harness de
  copia limpia exigido por la slice de entrega. Ese harness se ha planificado como trabajo
  de producto, aunque Factory ya posee el runner de reproducibilidad: falta resolver esa
  integración sin presentar tests ordinarios como prueba de todas esas condiciones.
- Referencias durables de autorización previa para `out_of_scope` y `quota_planning`, tal
  como exige el contrato final. La intención está en los datos/instrucciones, pero el flujo
  actual no la ha materializado en el formato de aceptación que requiere el cierre.
- Continuidad automática desde planning hasta una ejecución con ese contrato completo.

Resolverlo requiere integrar el binding y sus evidencias con el workflow existente. No se
ha publicado un contrato parcialmente cubierto ni marcado el proyecto como validado.
El presupuesto agotable del mismo run tampoco desaparece mientras se resuelve este hueco.

## Codex App, MCP y comprobaciones

El usuario realizó selección, reenvío pausado e inspección del proyecto activo en dos
chats. Se conserva la [evidencia de Codex App](evidence/end-to-end-12-codex-app.json), que
identifica esas pruebas como aportadas por el usuario y añade consultas nativas directas.

El launcher MCP instalado inició el worker real y se desconectó; otra conexión observó el
mismo run y su progreso. Tras completar planning, `factory_status` nativo en esta conversación
confirmó también el mismo run, `implementation_boundary` y las 13 llamadas. Sigue pendiente
la observación humana de trabajo activo/decisiones/entrega en la UI. No existe informe final
del producto que pueda abrirse todavía.

El plugin está actualizado: `software-factory@personal`, caché
`0.1.0+codex.20260911120741`. No hace falta reinstalarlo manualmente. Para consultar el estado
en un chat nuevo del mismo host:

> Usa Software Factory. Consulta el proyecto p_3084354cc76d1c23 y muestra su fase, run,
> presupuesto consumido y el binding de verificaciones que falta. No crees otro proyecto
> ni reinicies su presupuesto.

Las pruebas anteriores de selección/reenvío no necesitan repetirse. El `blockers: []` de
un cliente antiguo no refresca cuota ni elimina el diagnóstico de binding de la versión
actualizada del servicio. La suite normal utiliza modelos simulados y repositorios
desechables; sus recibos `project_verified` no se atribuyen a este piloto real.
