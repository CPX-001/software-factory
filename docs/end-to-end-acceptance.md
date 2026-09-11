# Paso 12: entregas reales y consulta humana final comprobadas

El [segundo piloto autorizado](automatic-pilot-result.md) completó la vinculación automática,
dos milestones y la entrega validada en su mismo run. El usuario confirmó después la consulta
humana del informe en otro chat, con versión, exclusiones y ubicación correctas.
Este documento conserva la evidencia del primer piloto, que llegó a `project_verified`
con la intervención de vinculación registrada.

**El mismo piloto real ha alcanzado `project_verified` y tiene entrega local.** Sus dos
milestones están cerrados y los tests independientes y el CLI pasaron desde una copia limpia.
La vinculación inicial de las pruebas al roadmap de esta primera instancia necesitó una
intervención revisada. La segunda instancia comprobó esa vinculación automática y su consulta
humana final, con las recuperaciones de planning documentadas. Ningún resultado demuestra
ausencia de intervenciones en cualquier proyecto nuevo.

La [corrección posterior de esa vinculación](planning-binding.md) está implementada con
autorización previa y revisión en el planning existente. Sus pruebas usan modelos simulados
y el mismo escenario de dos milestones. No se atribuye esa transición automática al piloto
real ya completado ni se han reiniciado sus contadores para volver a probarlo.

La [evidencia real final](evidence/end-to-end-12-real-delivery.json) conserva commits,
recibos, uso, intervenciones y recuperación. La [consulta nativa y repetición](evidence/end-to-end-12-delivery-native.json)
demuestra acceso al resultado mediante las tools de esta conversación, sin nuevo consumo.
Los [análisis anteriores](evidence/end-to-end-12-real-model.json) y bloqueos se conservan
como historia; ya no describen el estado actual.

## Instancia y resultado

- Proyecto: `p_3084354cc76d1c23`, Records end-to-end pilot.
- Producto: `/home/cpx/.local/share/software-factory/pilots/records-v1/product`.
- Run lógico/continuation: `b5774d1c-550d-49da-89df-a40749fa6e97`.
- Proceso de análisis e implementación: `9a2490b3-508b-4b60-bbb2-7aacf45d2901`.
- Proceso de recuperación del informe: `13f67813-17e6-408b-8230-c385fa5b451f`.
- Commit aceptado: `d2cd9261e0a09cf7a644d2eef85c006a4303da96`, en `factory/accepted`.
- Recibo: `sha256:5dc1fd473da105626922b9353e396fee2c1098fbd7274c706cb147da0e36fe9f`.
- Entrega: `product/.factory/deliveries/<recibo>/REPORT.md`, con código en `source/`.

El HEAD de la rama inicial sigue en el commit de recursos independientes
`4cb7db9823a10caceba52512d490d7fcd7942364`. Factory no cambió esa rama a la implementación:
el código entregado está en el commit aceptado y la copia de entrega. No hay push,
publicación, despliegue ni cambios de arquitectura automáticos.

El piloto anterior `p_1f2f88a25a7f899c` apuntaba a un directorio temporal de otro entorno
que no pudo recuperarse. Se creó una sola instancia persistente del escenario existente,
sin copiar estado ni credenciales de otra máquina. No se creó otro piloto para eludir un fallo.

## Recorrido real y consumo

| Fase | Llamadas | Tokens | Resultado |
| --- | ---: | ---: | --- |
| Discovery | 2 | 15.737 | Completado con modelo real |
| Arquitectura | 4 | 48.022 | Baseline aceptada, revisión 1 |
| Planning | 7 | 120.882 | Roadmap real: 2 milestones, 3 slices, 9 gates |
| Implementación | 4 | 84.038 | 3 slices aceptadas; incluye una corrección de respuesta |
| Refinamiento/preparación | 0 | 0 | Las slices ya estaban preparadas |
| Validación final y recuperación del informe | 0 | 0 | Checks deterministas, recibo y entrega |
| Total | **17** | **268.679** | Sin consumo desconocido |

Modelo `gpt-5.6-terra`, esfuerzo `low`, OpenAI, autenticación ChatGPT y runtime/SDK 0.147.0.
El usuario autorizó explícitamente retirar la reserva y ampliar lo necesario. La misma
continuation registra una ampliación de 20 a 30 llamadas, 250.000 a 500.000 tokens, dos a
tres slices y de 1.800 a 14.400 segundos desde su inicio original. Conserva las 13 llamadas
ya consumidas y su deadline anterior. Los límites individuales siguen en dos intentos,
600 segundos y 50.000 tokens. La reserva efectiva es 0 %; agotamiento real y telemetría
desconocida siguen bloqueando. No hubo cambio de proveedor, facturación alternativa ni
reinicio de contadores. Factory no gestionó refills de la cuenta.

## Aceptación independiente y reproducibilidad

Los 19 tests originales preceden a cualquier implementación: diez del resumen, cuatro
del ranking y cinco del CLI. Sus hashes en `pilot-contract.json` permanecen intactos.
Se añadieron antes de implementar seis tests para condiciones ya exigidas por el roadmap:
reutilizar los módulos reales, conservar los oráculos y ejecutar los comandos de `USAGE.md`.
No se sustituyeron las pruebas originales ni se escribió el producto fuera del worker.
La integración local observa las funciones reales; no usa mocks de servicios.

El [roadmap real congelado](../pilots/records-v1/accepted-plan.json) y su
[contrato de verificación](../pilots/records-v1/verification-binding.json) están versionados.
La aceptación final acredita 33 criterios mediante 21 bindings de seis procedimientos
distintos. Quince bindings reutilizan resultados idénticos sobre el mismo código, runner,
intérprete y perfil. Los recibos históricos de milestones no sustituyen la ejecución final.

Los gates reutilizan `Verifier`, `export_commit` y `LinuxSandbox`. Una slice congela su
árbol Git exacto sin publicarlo; el cierre final exporta el commit aceptado. Python de sistema
usa `-I -S`, solo biblioteca estándar, red deshabilitada, workspace de solo lectura y entorno
temporal vacío. No hereda `.venv`, secretos, bases residuales ni estado interno de Factory.
Los oráculos independientes están fijados por hash y fuera de los permisos de escritura.
Cambios de código, definición o entorno pertinente invalidan la evidencia correspondiente.

Los comandos documentados se ejecutaron durante la slice de entrega. Desde su `source/`,
la entrada comprobada es:

```bash
/usr/bin/python3 -S category_report.py examples/valid.json
```

Salida exacta:

```json
[{"category":"food","count":2,"total":12},{"category":"books","count":1,"total":7}]
```

El informe incluye procedimientos para repetir las pruebas. La entrega contiene código,
fixtures declaradas, contrato, recibo y runner versionado; no contiene credenciales, bases
internas ni transcripts. El informe queda fuera del código aceptado y no modifica el commit.
El recibo garantiza esas condiciones y versión, sin prometer despliegue, ausencia universal
de vulnerabilidades ni soporte para otros stacks.

## Intervenciones y recuperación

Las correcciones anteriores de discovery/planning están en la evidencia de análisis:
autorización efectiva en el contexto, rechazo de ciclos de cierre y revisión acotada de la
misma propuesta con las garantías reales del controller.

Esta continuación corrigió los bloqueos restantes:

1. La vinculación conserva ahora la ampliación autorizada como delta sobre el mismo ledger.
   Reanudar no amplía límites. El primer inicio tras el binding conserva el proceso de análisis.
2. Los gates de slice y milestone pueden reutilizar el runner limpio cuando lo exige el
   contrato. Un unittest independiente fijado por hash puede acreditar el harness sin exigir
   otro sistema de tests.
3. Las exclusiones ya expresadas por el usuario pueden referenciar mensajes exactos de
   discovery anteriores al plan, congelados en el contrato. No se creó un `accept` ficticio.
4. El worker de documentación solo declaró un criterio aunque los checks pasaron. Factory
   rechazó el cierre y corrigió la respuesta dentro del segundo intento autorizado.
5. Tras guardar el recibo, el informe falló al asumir que toda autorización tenía un ID de
   decisión. Se corrigió el proyector y recuperó por `factory_resume`: cero inferencias,
   verificaciones repetidas o cierres duplicados. El nuevo ID de proceso protege frente al
   worker antiguo y conserva el mismo run lógico, consumo y deadline.

La vinculación revisada al roadmap fue una intervención de integración registrada. El helper
del piloto rechaza un plan diferente y no presenta su mapping como una solución general
automática. Los criterios originales permanecieron fijos. La remediación final automática
no estaba autorizada y no se utilizó; su límite general sigue siendo un único ciclo
persistente cuando se autoriza por separado.

## Reproducción y Codex App

Para inspeccionar esta instancia sin iniciar inferencia:

```bash
.venv/bin/python scripts/smoke_continuation.py \
  --prepared "$HOME/.local/share/software-factory/pilots/records-v1/report.json" --installed
```

El mismo smoke informa del PASS independiente a partir de las pruebas del recibo y verifica
selección/reconexión. `report.json` proyecta el estado; `observations/` conserva cada observación.
Una petición repetida de validación conserva recibo, commit, proceso y consumo.

Para recrear únicamente la preparación, usa un directorio que todavía no exista:

```bash
.venv/bin/python scripts/smoke_continuation.py --from-discovery \
  --directory /ruta/nueva/records-v1 --model gpt-5.6-terra --effort low
```

La preparación no precarga arquitectura/planning, no autoriza inferencia y nunca sobrescribe
un piloto. El plan resultante necesita un binding revisado: el de esta instancia no se
aplica silenciosamente a otro roadmap. La nueva opción `--automatic-binding` prepara los
oráculos y la propuesta de autorización para que el planning normal produzca esa vinculación.
Su segunda ejecución real fue autorizada posteriormente y está documentada en
[el resultado del piloto automático](automatic-pilot-result.md); ya aceptó el plan y entregó
el producto verificado. No hay autorización implícita para crear más instancias con inferencia.

El usuario ya comprobó selección, reenvío pausado e inspección en dos chats; esa
[evidencia de App](evidence/end-to-end-12-codex-app.json) no necesita repetirse. Las tools
nativas de esta conversación consultaron ejecución activa y resultado final. El cliente
MCP instalado se desconectó mientras el controller avanzaba entre slices, milestones y
validación. Esto no equivale a observar todos los pasos en la UI humana.

El primer piloto utilizó el plugin `0.1.0+codex.20260911131027`. La comprobación humana final
se realizó sobre la segunda instancia, con el launcher actualizado. El usuario confirmó
el resultado del siguiente mensaje en otro chat:

> Usa Software Factory. Selecciona el proyecto p_ba973102351c9eac y muestra la versión
> validada, sus exclusiones y la ruta del informe de entrega local. No inicies ni
> reanudes ejecuciones.

La [evidencia de esa consulta](evidence/end-to-end-12-auto-codex-app.json) conserva la respuesta
aportada por el usuario y su contraste con el estado actual. No hace falta repetirla.

Antes de la corrección de vinculación, la suite completa pasó **310 tests en 505,201 segundos**, sin fallos, errores ni omisiones
y sin inferencia. Se conservan el [log](evidence/end-to-end-12-final-tests.txt) y el
[manifiesto de fuentes](evidence/end-to-end-12-final-checks.json). También pasan compilación,
whitespace e instalación/validación del plugin. Los tests con modelos simulados cubren
además fallo integrado, remediación global acotada, obsolescencia, ausencia de capacidades
y recuperación. Esos casos no se atribuyen al piloto real, que no necesitó una remediación
final del producto.

La corrección posterior de vinculación pasa **321 tests en 533,917 segundos**, sin fallos,
errores ni omisiones y sin inferencia real. Se conservan el [nuevo log](evidence/end-to-end-12-automatic-binding-tests.txt)
y la [evidencia con hashes](evidence/end-to-end-12-automatic-binding.json). El plugin instalado
`0.1.0+codex.20260911135342` expone la nueva autorización. El mismo smoke instalado, en modo
consulta, mantiene el commit, recibo y consumo del primer piloto real. Esa comprobación
inicial no validaba la nueva transición; su ejecución real y consulta humana posteriores
quedan registradas en el informe del segundo piloto.
