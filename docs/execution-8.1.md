# Paso 8.1: cuota y ejecución real

Comprobado el 11 de septiembre de 2026 (Europe/Madrid). Se ejecutó **un solo smoke con
modelo**, en un repositorio desechable separado. No se usó Factory para desarrollar Factory.

## Causa y corrección

El SDK instalado es `openai-codex==0.147.0` y arranca su runtime incluido `codex-cli 0.147.0`,
SHA-256 `cb0a15567e9a60a5820d54b0f6ae86d504dc3805c1eab21a47f70e3eb7b73a40`.
El ejecutable de terminal es **0.149.1**, otro binario. Ambos están identificados por ruta,
versión y hash en el diagnóstico; consultar solo `codex --version` habría sido insuficiente.

La inicialización del SDK, `account/read` y `model/list` funcionaban dentro del aislamiento.
La autenticación efectiva era ChatGPT. La llamada correcta, `account/rateLimits/read`,
recibía **InternalRpcError -32603** con la causa
`failed to fetch codex rate limits: error sending request`. El filtro externo de la sesión
impide enviar por sockets (`EPERM`) y resolver `chatgpt.com` (`EAI_AGAIN`). Al permitir la
conexión del comando controlador, **sin alterar el sandbox de Factory**, el mismo runtime,
configuración y método devolvieron cuota válida. No era un método ausente ni hacía falta
actualizar el SDK. El adaptador anterior ocultaba esta causa como `quota_unknown`.

Se conserva ahora la clasificación sanitizada y el siguiente paso en status/inspect.
Se validan vista individual, buckets y ventanas opcionales, se rechaza coerción de tipos,
y la reserva se comprueba sobre `codex`, nunca sobre el bucket más libre. Spark/otros
medidores requieren un mapa documentado que esta modalidad aún no soporta. No se usan
claves API, créditos, resets, proveedores alternativos ni inferencia para consultar cuota.

La configuración del runtime se genera en un HOME privado: proveedor OpenAI, login
ChatGPT, permisos read-only, sin MCP/plugins/shell/subagentes. No hereda claves API ni
configuración de proveedor. En este host no había proxies ni variables de CA configurados.
Los tests de producto siguen en namespaces sin red, sin credenciales ni SQLite de Factory.

Diagnósticos conservados:

- [Consulta restringida, error y causa sanitizados](evidence/execution-8.1-quota-restricted.json).
- [Misma consulta con conectividad y aislamiento intacto](evidence/execution-8.1-quota-connected.json).
- [Comprobación posterior del parser estricto, sin inferencia](evidence/execution-8.1-quota-final.json).
  Marcó 74% de uso compartido; esa variación posterior tampoco se atribuye al smoke.

Reproducción sin inferencia: `scripts/diagnose_execution.py --model gpt-5.6-terra --effort low`.
Se usa el [SDK Python con runtime fijado](https://learn.chatgpt.com/docs/codex-sdk) y los
[métodos oficiales de cuenta de App Server](https://learn.chatgpt.com/docs/app-server).
Las excepciones no se vuelcan a informes: se conservan tipo, código RPC y causa conocida.

## Smoke real

Se amplió una utilidad Python existente para validar registros y devolver count/total y
agregados por categoría, conservando la normalización existente y sin mutar entradas.
La solución de `records.py` procede del modelo, sin ediciones posteriores del controlador
fuera de aplicar la propuesta. `record_rules.py` y `test_records.py` quedaron intactos.
Los diez tests y dos ejemplos fijos se prepararon y versionaron **antes** del worker.

| Dato | Resultado |
| --- | --- |
| Proyecto | `p_48f4da23ac158c1c` |
| Repositorio | `/tmp/factory-records-smoke-mlzxlicn/product` |
| Run | `af2065af-5c2a-4b02-81ed-d21f63d169bb` |
| Ejecución | `bdddd848-b760-47c1-8082-61c2ff1b2fe1` |
| Thread | `01a08d9a-43de-7902-ad0a-acc46e1c5324` |
| Turno | `01a08d9a-4402-7033-a657-8eaac12e6abc` |
| Modelo/esfuerzo | `gpt-5.6-terra` / `low`, ofrecidos por el runtime |
| Auth/runtime | ChatGPT / SDK y runtime incluido 0.147.0 |
| Revisiones | Arquitectura 1, planning 1; hashes completos en el recibo |
| Definición de verificación | `sha256:e84a58565ed59ce0b8464acfac4e8d064ba6eaac8ab840b333783a8f0a18a241` |
| Intentos | 1 implementación; no hizo falta reparación |
| Presupuesto | Máximo 2 intentos, 180 s, 8.000 tokens observados, reserva 25% |
| Retorno de lanzamiento MCP | 0,109 s; cliente cerrado en estado queued |
| Tiempo observado | Aproximadamente 18 s hasta consultar el checkpoint |
| Verificación de Factory | 10 unittest y 2 casos fijos: PASS, ambos procesos con exit 0 |
| Uso del turno | 6.973 input + 525 output = **7.498 tokens**; reasoning reportado: 102 |
| Cuota compartida | `codex` 73% usado antes y después; secondary ausente |
| Estado final | Checkpoint intencionado de una slice |
| Rama | `factory/slice-bdddd848-b760-47c1-8082-61c2ff1b2fe1` |
| Commit aceptado | `3a1a02d88d9482b64d67cd433e0517a10ad4c012` |
| Código verificado | `sha256:46753b5c0bbfab0dcc97df1e7aa6b6b2d6b3e49327f25aac151eb9d53e98490c` |

[Informe completo con propuesta, verificaciones, logs, uso y recibo](evidence/execution-8.1-smoke.json).
La rama original permanece en `ccc9ec2dfd9ee7778ff99df264a726bfcd90da37`.
El worktree queda bajo `.factory/worktrees/bdddd848-b760-47c1-8082-61c2ff1b2fe1`.
Se comprobó correspondencia propuesta → commit → código verificado → recibo, una sola
aceptación, proceso parado y retirada de la copia privada de credenciales.

El porcentaje de cuenta es compartido y puede llegar redondeado/con retraso; su estabilidad
no significa consumo cero. Tampoco se atribuye al smoke la variación 72→73 observada entre
diagnósticos anteriores, pues hubo actividad concurrente de Codex. El presupuesto de tokens
actúa al recibir telemetría, no como garantía de corte exacto dentro del servicio.

## Qué se probó y qué queda pendiente

La suite final completa pasó **210 tests en 104,997 s**, sin cuota:
`.venv/bin/python -m unittest discover -v`.
[Log completo](evidence/execution-8.1-tests.txt). También pasaron compileall, diff-check y
los validadores de plugin/skill. La suite necesita permiso de sockets del host para MCP;
las verificaciones de producto conservan el aislamiento propio de Factory.

- **Modelo real + MCP real:** contexto de archivos existentes, reutilización del helper,
  propuesta estructurada sobre la ruta autorizada, aplicación por Factory, checks aislados,
  aceptación/publicación y finalización después de cerrar el cliente de lanzamiento.
- **SDK simulado sin cuota + verificaciones reales:** solicitud de contexto adicional
  previamente omitido, reutilización del hilo, errores de aplicación y FAIL → reparación →
  PASS, además de presupuestos, aislamiento y recuperación. El smoke real no solicitó
  `read_paths` ni necesitó reparar; no se forzó un fallo para provocar otro turno de pago.
- **Plugin instalado:** tras actualizar mediante el flujo cachebuster/reinstall, una nueva
  conexión al entrypoint MCP instalado expuso 10 tools y leyó el checkpoint y el commit.
  [Evidencia de esa conexión](evidence/execution-8.1-installed-mcp.json).
- **Codex App:** la consulta efectuada con la conexión antigua devolvió
  `Unsupported schema version: 6`; seguía cargando el proceso anterior. No se presenta la
  prueba por cliente MCP como prueba de la UI. Hace falta una conversación nueva para
  que la app cargue el plugin reinstalado y sus nuevas tools.

La copia preparada `p_131e6e1400955d7a`, en
`/tmp/factory-records-smoke-os694j2y/product`, está registrada y autorizada con los mismos
límites, pero **no se ha ejecutado**. No arranca hasta una petición explícita y vuelve a
comprobar cuota. En una conversación nueva de Codex App:

> Usa Software Factory para implementar la próxima slice preparada del proyecto p_131e6e1400955d7a.

Para consultar el resultado ya obtenido, sin consumir modelo:

> Usa Software Factory para mostrar el resultado y las verificaciones del proyecto p_48f4da23ac158c1c.

La preparación/arquitectura/planning del escenario fueron sintéticos y pasaron por los
contratos y publicación existentes. Esta prueba no valida esas fases con modelos reales,
ni prueba toda la semántica del producto. Continúan pendientes otros lenguajes/harness,
revisiones especializadas, medidores separados y el futuro loop multislice. No se añaden
refinamiento, revisión arquitectónica automática, cierre de milestones ni despliegue.
