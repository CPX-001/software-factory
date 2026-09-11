# Paso 12: preparación verificable; aceptación end-to-end pendiente

**No se ha completado el recorrido real.** El preflight del único piloto nuevo conserva
`quota_reserve`: 92 % usado, reserva del 25 %. Se ha instalado el plugin y comprobado su
launcher por MCP real, sin turnos de modelo ni interacción con la UI de Codex App.

La suite completa pasa **296 tests en 460,943 segundos**, sin fallos, errores ni skips.
Se conserva el [log](evidence/end-to-end-12-tests.txt), el
[manifiesto de fuentes y comprobaciones](evidence/end-to-end-12-checks.json), el
[informe sanitizado del piloto](evidence/end-to-end-12-pilot.json) y la
[reconexión con reenvío idempotente](evidence/end-to-end-12-reconnect.json).

## Instancia y reproducción

Se investigó el piloto anterior `p_1f2f88a25a7f899c`: el informe de los pasos 9/10 apunta a
`/tmp/factory-continuation-smoke-bc7lwhrv/product` y a un registro bajo el mismo directorio.
Ambos faltan. Antes de preparar este paso tampoco existía el registro por defecto de Factory.
La búsqueda de `registry.sqlite3` en `/home/cpx`, `/tmp` y `/var/tmp` no encontró un registro
recuperable del piloto anterior; no se tiene acceso a otros hosts. No se ha copiado estado
ni reconstruido su identidad. Esta es **una instancia nueva del escenario de registros**.

- Proyecto: `p_3084354cc76d1c23`.
- Directorio persistente: `/home/cpx/.local/share/software-factory/pilots/records-v1`.
- Producto: `product/`; registro: `/home/cpx/.local/state/software-factory/registry.sqlite3`.
- Commit inicial de fixtures: `9af30b4ae36f421f6a41eb2b77011b9fbb303a65`.
- Mensaje estable: `records-v1-discovery-once`.
- Run/continuation: **ninguno**; discovery tiene un mensaje pendiente y el proyecto está pausado.

Para recrear la preparación desde el checkout de Factory **solo cuando ese directorio no exista**:

```bash
.venv/bin/python scripts/smoke_continuation.py --from-discovery \
  --directory "$HOME/.local/share/software-factory/pilots/records-v1" \
  --model gpt-5.6-terra --effort low
```

Se amplía el driver existente; no se añade otro orquestador. El comando no acepta una ruta
dentro de Factory y rechaza cualquier directorio existente, incluso con cambios sin aceptar.
No hay `--force`. El contenido de [brief](../pilots/records-v1/brief.md), los oráculos del
[fixture existente](../scripts/execution_smoke_fixture.py) y los
[tests de CLI](../pilots/records-v1/test_product_cli.py) forman parte del código del escenario.
`pilot-contract.json` fija sus hashes antes del worker. `factory_source` identifica el commit
base del checkout; el manifiesto de evidencia de este paso identifica además los archivos
locales nuevos/modificados. SQLite, observaciones, credenciales y producto generado quedan fuera
del repositorio de Factory.

Para inspeccionar/reintentar el preflight de **esta misma instancia**, sin reconstruirla:

```bash
.venv/bin/python scripts/smoke_continuation.py \
  --prepared "$HOME/.local/share/software-factory/pilots/records-v1/report.json" --installed --run
```

La carga comprueba registro, proyecto y contrato. Reutiliza el mismo ID del mensaje y no
reinicia contadores. `report.json` es la proyección más reciente; `observations/` conserva
cada observación. El `--run` termina con código 1 al bloquear; **hoy no inicia inferencia**,
ni siquiera con cuota disponible, porque falta el control agregado de las fases de análisis.
Omitir `--run` inspecciona el mismo estado sin consultar cuota. Esto no es todavía una
reanudación end-to-end operativa.

## Producto y aceptación independiente

Es una utilidad local de biblioteca estándar: resumen validado y ranking determinista de
registros JSON, con API pública y `python category_report.py examples/valid.json`.
El primer hito entrega el resumen reutilizable; el segundo compone el ranking y su entrada
de terminal. Son dos objetivos de producto y al menos dos slices dependientes, no un plan
precargado. El helper existente `record_rules.py` se conserva; faltan las dos implementaciones.

Los ejemplos incluyen datos válidos, orden invertido, vacío, importe negativo, bool y JSON
malformado. Los 10 oráculos de resumen y 4 de ranking existentes cubren además Unicode,
campos/tipos incorrectos, NaN/infinito, desempate y ausencia de mutación. Cinco nuevos tests
de CLI fijan salida exacta, repetición determinista, entrada inválida sin resultado parcial,
vacío y composición con la API. El brief comunica todos estos requisitos al workflow.

Se reutilizarán el runner, el export limpio y los gates de Factory. Los checks del contrato
son plantillas independientes: su vínculo a los gates de un plan real está **pendiente**.
No se inventan IDs de planning ni se marcan fases como completadas. El resultado de aceptación
independiente es **NOT_RUN**; no existe commit de producto validado, recibo final ni entrega.

## Evidencia obtenida y límites de integración

| Superficie | Resultado de este paso |
| --- | --- |
| Plugin local | Instalado/habilitado `software-factory@personal`, versión 0.1.0; skill, manifest y launcher coinciden con la caché instalada |
| MCP real del plugin | Lista de 10 tools, selección explícita, envío del brief pausado, desconexión y selección conservada al reconectar |
| Workflow del piloto | Inicialización y mensaje encolado; ninguna fase completada y ningún run creado |
| Modelo real | Cero inferencias; autenticación ChatGPT, modelo solicitado `gpt-5.6-terra`, esfuerzo solicitado `low` comprobados solo en el preflight de ejecución |
| Consumo por fase | Discovery: 0 turnos completados; arquitectura/planning: 0 llamadas; ejecución/refinamiento: no iniciados. Tokens de análisis no registrados por los adaptadores actuales |
| UI de Codex App | Pendiente; esta sesión no expone tools nativas de Factory ni control de esa UI |

No se han inventado respuestas humanas ni reparaciones. La única entrada enviada es el
brief declarado como datos del piloto, y permanece sin procesar. La suite normal conserva
los tests con modelos simulados que llegan a `project_verified`; no sustituyen este piloto.

La revisión detectó límites que no deben ocultarse detrás de la cuota:

1. `continuation_store.budget` empieza a contar después de planning. Discovery, arquitectura
   y planning usan adaptadores distintos, sin ese ledger de tokens/deadline ni la reserva
   efectiva de ejecución. Una cota vigilada desde un cliente MCP no resolvería el problema:
   desconectar el cliente dejaría de controlar el gasto.
2. `Controller.stop_reason` conserva `implementation_boundary`. La política tipada se liga
   al plan ya aprobado. No existe autorización previa de todo el recorrido que materialice
   los checks tras planning y arranque ejecución automáticamente. Esa autorización inicial
   puede ser legítima; no se ha demostrado el recorrido sin intervención que pide el paso 12.
3. Los adaptadores de análisis fijan modelo, pero no aplican el esfuerzo de la política de
   ejecución. No se afirma que `low` sea efectivo en esas fases.

Resolverlo exige integrar autorización, consumo y despacho en el controller/journal existente
y vincular los oráculos al planning real; no basta con cambiar un flag o ejecutar cada fase
desde el script. No se implementó esa ampliación importante durante el bloqueo de cuota,
ni una capa paralela para aparentar éxito. El caso queda reproducible. Se conservan los
límites anteriores: 2 unidades, 4 llamadas, 300 segundos y 30.000 tokens; se declaran como
cota requerida de todo el piloto, pero **no se presentan como control global implementado**.
No se han ampliado ni renovado. Esa cota tampoco garantiza que alcance para todas las fases.

La preparación corrige el uso exclusivo de `/tmp` para un piloto reanudable y separa creación
de reanudación; las pruebas rechazan sobrescritura/contrato modificado y cualquier inferencia
desde el driver mientras falte el control global, incluso simulando cuota disponible.
No se han añadido las ampliaciones de presupuesto, dependencias o invalidación de
`future-improvements.md`; la ubicación persistente responde al encargo actual.

## Mensajes exactos para comprobar la superficie pendiente

El plugin está instalado en este host. No hace falta instalarlo manualmente. Abre una
conversación nueva de Codex App en el mismo entorno para que cargue sus tools.

1. «Usa Software Factory. Selecciona el proyecto p_3084354cc76d1c23 y muestra su ruta, fase,
   estado y run actual. Mantén su pausa y no inicies inferencia.»
2. «Reenvía como datos del piloto el contenido de
   /home/cpx/.local/share/software-factory/pilots/records-v1/product/PILOT.md a ese proyecto
   usando request_id records-v1-discovery-once. Mantén la pausa y comprueba que no se crea otro run.»
3. Cierra esa conversación y, en otra: «Usa Software Factory. Consulta el proyecto activo
   sin crear ni reanudar ninguno. Confirma si sigue siendo p_3084354cc76d1c23 y muestra
   su run y decisiones pendientes.»

Estos mensajes comprueban selección, envío y reconexión desde la UI; no completan la aceptación
con modelo real ni la prueba de una respuesta humana a una decisión real. No se fabrican
preguntas para demostrarla. El informe final solo podrá comprobarse cuando exista.

La instalación se contrastó con la [documentación oficial de plugins](https://developers.openai.com/plugins/build/plugins)
y con el CLI local. Las pruebas de transporte no prueban por sí mismas su presentación en
la [superficie MCP de Codex](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
