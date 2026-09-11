# Segundo piloto real: detenido en planning

La segunda ejecución autorizada recorrió discovery y arquitectura con modelo real, pero
su planning no superó el gate determinista. **No tiene código implementado, aceptación
independiente ni entrega. El goal de autonomía end-to-end sigue pendiente.** La primera
[entrega real](end-to-end-acceptance.md) conserva su commit y recibo; allí la vinculación
inicial del roadmap necesitó una intervención revisada.

La [evidencia sanitizada](evidence/end-to-end-12-auto-real.json) identifica llamadas,
contratos, recuperación, consumo y límites. El escenario sigue siendo Records,
Python con biblioteca estándar: resumen de registros, ranking, entrada CLI y documentación.
Los mismos 25 tests independientes y fixtures quedaron versionados antes de discovery.
No se precargaron arquitectura, plan ni implementación.

| Fase | Llamadas contabilizadas | Tokens observados | Resultado |
| --- | ---: | ---: | --- |
| Discovery | 1 | 9.370 | Requisitos obtenidos |
| Arquitectura | 2 | 23.585 | Baseline aceptada |
| Planning | 7 | 159.190 | Bloqueado; incluye un rechazo previo a generación |
| Implementación y validación final | 0 | 0 | No iniciadas |
| Total | **10** | **192.145** | Sin consumo pendiente de clasificación |

El modelo fue `gpt-5.6-terra`, esfuerzo `low`, SDK/runtime 0.147.0, autenticación ChatGPT.
La autorización concreta fue 20 llamadas, 1.800 segundos y 500.000 tokens agregados.
Se conservó la autorización anterior de reserva 0 %. Fast se solicitó con
`service_tier="priority"`, ofrecido por ese modelo, para cada thread y turno. No llegó
notificación del nivel aplicado y no se ha medido una aceleración. No se cambió proveedor,
cuenta, facturación ni configuración global de Codex.

El proyecto es `p_ba973102351c9eac`, en
`/home/cpx/.local/share/software-factory/pilots/records-auto-v1/product`.
Su run es `50fe4228-5bee-4f71-a660-dcc6357768db` y su continuation
`2abc197c-eac7-497a-850e-d4c5379cf9cf`. La recuperación conservó ambos IDs, todas las
llamadas y el deadline original. El tiempo de corrección de Factory no lo reinicia.
En la observación final esa ventana de 30 minutos ya había vencido durante las correcciones
y la suite local. Se conservan 10 llamadas y 192.145 tokens; la parada original fue el gate
de planning y su recuperación agotada, no la cuota.

## Defectos e intervenciones

El proveedor rechazó el esquema de planning por usar `oneOf`. Se sustituyó por `anyOf`
con dos objetos cerrados, conservando exactamente una forma de autorización por exclusión.
Factory clasifica únicamente el error explícito HTTP 400 `invalid_json_schema` sobre
`text.format.schema`: mantiene la llamada contabilizada, el error y el uso SDK sin reportar.
No fabrica una medición de cero tokens. Los demás fallos con uso desconocido siguen
bloqueando inferencia. Tras corregir Factory se reanudó el mismo run, sin repetir discovery
ni arquitectura y sin editar SQLite o la propuesta del producto.

El plan asignó `risk_contract_drift` al componente `cmp_category_report`. El contrato exige
una milestone o slice del propio plan, con referencia inversa al riesgo. Los errores
`risk_owner:risk_contract_drift` y `risk_owner_link:risk_contract_drift` persistieron tras
las dos reconciliaciones permitidas, aunque el crítico devolvió una revisión sin hallazgos.
El gate rechazó la propuesta; no existe roadmap aceptado. Quedaban llamadas agregadas,
pero estaba agotada la recuperación de planning: una corrección normal y una recuperación
adicional, con límite persistente de ocho llamadas de fase, incluidos errores.

Las instrucciones y el esquema explican ahora esa propiedad y ambos errores. La regresión
comprueba que un componente no puede ser propietario y que un ID válido sin referencia
inversa tampoco pasa. Se conservaron el gate y los límites. **Esta aclaración posterior
no se probó con otra inferencia real y no desbloquea automáticamente el caso guardado.**
Cambiar código, prompt, commit o nombre del fallo no concede otra recuperación.

El resumen del smoke también omitía el motivo de este bloqueo. Ahora copia los errores de
análisis del estado y separa uso SDK ausente de rechazos clasificados previos a generación.
La consulta instalada comprobó la corrección sin crear llamadas ni un run nuevo.

## Consulta y reproducción

La [tool nativa inició el run](evidence/end-to-end-12-auto-native-start.json) en esta
conversación y [mostró el bloqueo](evidence/end-to-end-12-auto-native-blocked.json).
El controller avanzó después de desconectar MCP. Un cliente nuevo con el plugin instalado
`0.1.0+codex.20260911141403` confirmó selección, mismo run y política efectiva. El servidor
MCP ya abierto en esta conversación conserva código anterior: para el cómputo corregido
de uso se utilizó un proceso nuevo del launcher instalado. Esto no es una observación humana
del informe en la UI. Las dos comprobaciones humanas anteriores de selección/envío pausado
siguen registradas y no necesitan repetirse.

Consulta comprobada, sin inferencia:

```bash
.venv/bin/python scripts/smoke_continuation.py \
  --prepared /home/cpx/.local/share/software-factory/pilots/records-auto-v1/report.json --installed
```

`report.json` y `observations/` conservan las proyecciones locales; no son una entrega aceptada.
El mismo comando con `--run` es el camino de reanudación, conserva consumo e historial y
respeta el bloqueo. Repetirlo no corrige un plan con recuperación agotada. Para avanzar falta
resolver esa propuesta por un nuevo esfuerzo expresamente autorizado y soportado; no se
ha añadido aquí un mecanismo que renueve sus contadores.

La [preparación reproducible](planning-binding.md) usa el mismo smoke y un directorio nuevo.
Preparar otra instancia no autoriza un tercer piloto real ni sustituye la reanudación.

Para consultar el caso desde otro chat, sin repetir las pruebas humanas ya realizadas:

> Usa Software Factory. Consulta el proyecto p_ba973102351c9eac. Muestra su fase, bloqueo
> y run actual. No inicies ni reanudes ejecuciones.

La observación humana de una entrega final sigue pendiente para el primer piloto; su mensaje
exacto está en [la entrega real](end-to-end-acceptance.md). Ninguna consulta al segundo
proyecto puede demostrar una entrega que todavía no existe.

La suite completa pasa **326 tests en 646,623 segundos**, sin fallos, errores ni omisiones,
y sin inferencia. El [log](evidence/end-to-end-12-auto-tests.txt) y el
[manifiesto de 89 fuentes](evidence/end-to-end-12-auto-checks.json) corresponden al código
`e7d9ba6`. Pasan también compilación, validación del plugin y comprobación de whitespace.
La suite normal usa modelos simulados; la aceptación real de esta segunda
instancia continúa bloqueada. Las correcciones no amplían lenguajes, dependencias,
arquitectura, paralelismo, publicación ni despliegue.
