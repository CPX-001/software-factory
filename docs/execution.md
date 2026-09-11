# Ejecución verificable y recuperable (pasos 8 y 8.1)

La autorización original conserva **una slice por run**. La migración inicial deja
`enabled=false`; actualizar el plugin, consultar status o reanudar discovery/planning
no habilita escritura. El paso 9 añade [continuidad y refinamiento justo a tiempo](continuation.md)
mediante autorización adicional explícita; la migración a schema 7 no la activa.
No hay cierre de milestones, arquitecto automático, paralelismo ni despliegue.

## Autorizar, lanzar y consultar desde Codex App

1. Seleccionar el proyecto registrado y consultar `factory_inspect(view="next_slice")` y
   `factory_inspect(view="verification")`.
2. Autorizar una vez `factory_execution_policy`: modelo/esfuerzo ofrecidos por el runtime,
   rutas relativas de producto que puede cambiar, archivos de contexto, hasta 3 intentos,
   duración total, tokens observados, bucket de cuota y reserva. La misma operación recibe
   una definición de checks tipados revisada, ligada a la arquitectura y plan actuales.
   No ejecuta un worker ni acepta shell, SQL o cambios de fase. Una ejecución abierta congela
   política y definición: el worker no puede rebajar controles ni ampliar permisos.
3. Pedir **«Usa Software Factory para implementar la próxima slice preparada»**.
   El plugin llama `factory_execute(project=…, request_id=…)`. Factory selecciona la slice
   y devuelve rápidamente `execution.id`, `run_id` y estado. Reusar `request_id` en reintentos.
4. Pedir «¿Cómo va la ejecución?» (`factory_status`) o «Enséñame las verificaciones y el
   resultado» (`factory_inspect(view="execution")`). La conversación puede cerrarse.
   No hace falta mantener un supervisor LLM ni consultas periódicas desde la app.
5. `factory_pause` solicita interrupción. `factory_resume` inspecciona y recupera la ejecución
   existente. Responder con `factory_answer` continúa automáticamente cuando corresponde.
   Un checkpoint es el límite intencionado de una slice; no requiere una decisión.

Las autorizaciones incluyen comandos tipados, no confirmaciones por cada comando.
La CLI usa los mismos casos de uso:

```bash
.venv/bin/python -m factory execution-policy /ruta/producto --policy policy.json --verification verification.json
.venv/bin/python -m factory execute /ruta/producto --request-id mi-solicitud-estable
.venv/bin/python -m factory status /ruta/producto
.venv/bin/python -m factory execution-show /ruta/producto
.venv/bin/python -m factory pause /ruta/producto
.venv/bin/python -m factory resume /ruta/producto
```

## Contratos de verificación y modalidad inicial

Planning proporciona gates, señales, checks en lenguaje natural y capacidades de harness.
La definición ejecutable **adicional** materializa esa estrategia mediante una autorización
explícita, sin alterar la revisión de planning ni pedir a otro LLM que la redescubra.
Sus esquemas cerrados están en `execution_contract.py`. Cada check identifica un gate,
índices de checks del gate y criterios de aceptación de la slice (base cero), timeout,
un mínimo de pruebas y un objetivo concreto. No se ejecuta el shell string del worker.

Se soportan inicialmente:

- `python_behavior`: `target="modulo:funcion"`, casos con `args_json` (array JSON) y
  `expected_json`. El runner de Factory importa el módulo y compara resultados con los
  valores fijados antes del worker. Un proceso que sale con 0 antes de acabar no es PASS.
- `python_unittest`: archivo `.py` exacto, número mínimo de pruebas. Cero pruebas,
  omisiones, expected failures, dependencias ausentes y timeout quedan sin validar.
- `specialist`: revisión obligatoria todavía no implementada. Bloquea antes del modelo;
  el ejecutor conserva `NOT_RUN` para evidencias de revisiones no disponibles.

El preflight detecta checks especializados y requisitos explícitos de Node, navegador,
Docker o pytest en el alcance, harness y runtime relevantes. También rechaza rutas de
implementación de otros lenguajes. Son controles conservadores sobre señales concretas,
no un clasificador semántico completo. Los gates futuros y el out-of-scope no se usan
para bloquear la slice. Esta es una limitación del ejecutor actual, no del objetivo futuro
de Factory; no se sustituye una comprobación obligatoria por un check Python trivial.

Por ejemplo, un check tipado para sumar:

```json
{"id":"addition","gate":"local_gate","kind":"python_behavior","target":"app:add",
 "cases":[{"args_json":"[1,2]","expected_json":"3"}],"min_tests":1,
 "timeout_seconds":5,"criteria":[0],"gate_checks":[0]}
```

No se ejecutan automáticamente gates de cierre de milestone o sistema. Se ejecutan los
gates locales e integración `before_slice`/`after_slice` definidos para la slice elegida.
La cobertura de TODOS sus checks y criterios es una precondición. Los gates de otros
alcances siguen pendientes. El PASS prueba esos casos sobre ese código, no todos los
requisitos globales ni una interpretación semántica completa de cada criterio.

El harness `during_slice` introducido por la propia slice puede no existir al empezar;
los demás consumidores exigen los archivos declarados. Factory valida sus rutas y
registra una revisión de sus hashes antes de verificar. Los tests existentes y los nuevos
ya incorporados quedan congelados durante las reparaciones: no pueden borrarse o volverse
triviales para conseguir PASS. Si la slice construye el harness, se exige además cobertura
con casos `python_behavior` independientes previamente fijados. Un test nuevo defectuoso
puede necesitar intervención; no se permite al worker relajar la definición.

## Aislamiento realmente aplicado

La primera implementación usa Linux con namespaces de usuario, montajes y PID, chroot,
`mount_setattr`, eliminación de capabilities, `no_new_privs` y libseccomp. Ejecuta una
prueba de capacidades antes de trabajar; si falla, bloquea. No hay bypass de sandbox o
aprobaciones ni fallback a ejecutar directamente en el cwd.

El runtime Codex recibe un HOME privado con copia del login existente y configuración
propia, el worktree de solo lectura, skills required de solo lectura y bibliotecas del
sistema. No ve SQLite, evidencias, configuración del controlador, otras carpetas de
usuario ni otros proyectos. Su entorno se reconstruye sin claves API heredadas. Conserva
la red necesaria para el servicio Codex; ningún código de producto se ejecuta allí.
Se deshabilitan MCP de Factory, plugins/apps, hooks, herramientas de shell, navegador,
otros modelos y subagentes. Seccomp permite threads pero impide procesos hijos.

**Codex implementa mediante propuestas de archivos estructuradas**; Factory valida todas
las rutas y aplica sus cambios al worktree gestionado. No es aún una sesión interactiva
con terminal de desarrollo. Puede solicitar exploración localizada por `read_paths`,
consumiendo un intento. Los archivos solicitados tienen prioridad sobre el contexto
automático; la respuesta explica rutas ausentes, no autorizadas, no UTF-8 o demasiado
grandes. Los conflictos archivo/directorio al aplicar propuestas vuelven al worker dentro
del mismo presupuesto de reparación; violaciones de permisos/harness siguen bloqueadas.
La cápsula contiene objetivo, scope, criterios, requisitos,
contratos/invariantes de los componentes, fuentes, dependencias, verificaciones y archivos
acotados; las reparaciones reutilizan el hilo propio de la slice. No se envía SQLite ni
el historial completo. Se soportan archivos UTF-8 y presupuestos de tamaño explícitos;
se bloquean enlaces simbólicos y archivos especiales.

Las verificaciones tienen otro namespace **sin red**, sin credenciales ni estado Factory,
worktree de solo lectura y `/tmp` privado. El controlador captura salida y exit code a
través de pipes y escribe la evidencia fuera del entorno del producto. Incluye comando,
perfil del entorno, duración, logs limitados a 24 KB, código SHA-256 y cobertura. Los tests
no pueden modificar SQLite ni suplantar recibos de aceptación. Los asserts siguen siendo
software: no se promete resistencia a todo programa deliberadamente diseñado para mentir
sobre su semántica; por eso las declaraciones/logs del worker jamás son certificados.

## Estado durable, límites y recuperación

`executions`, `execution_attempts`, `execution_requests`, definiciones, eventos y recibos
se guardan en SQLite mediante transacciones cortas. El lock de proceso de Factory excluye
controladores concurrentes; el índice único excluye ejecuciones abiertas simultáneas.
Cada intento tiene ordinal y token de fencing, runtime/thread/turn IDs y resultado propio.
Un worker sustituido no puede guardar resultados. Una lease mantenida por el supervisor
fuera del sandbox y la identidad PID/inicio/boot evitan confundir desconexión con muerte.

El resultado se guarda ANTES de aplicar cambios. Recuperar una respuesta ya guardada
reaplica idempotentemente y vuelve a comprobar el worktree, sin otro turno de implementación.
Una lease todavía viva impide relanzar; el supervisor impone duración y mata el namespace
al finalizar. No hay reanudación automática del daemon tras reiniciar el host: se usa resume.
Si SQLite conserva un turno iniciado pero no su respuesta, Factory consulta ese thread/turn
exacto mediante el SDK antes de gastar otro intento. Un resultado completado recuperable
se guarda y verifica; un estado imposible de inspeccionar queda bloqueado. No se infiere
que un turno fracasó solo porque se cerró la conexión.

Hay **1 implementación + hasta 2 reparaciones**, incluyendo respuestas inválidas,
solicitudes de más contexto y llamadas interrumpidas. Los contadores no se reinician.
Se detiene antes si el fallo se repite sin cambios de código. Pausa, fallo del producto,
infraestructura, decisión, cuota, validación pendiente y agotamiento de presupuesto se
conservan como estados diferentes. Una pausa durante un turno usa `turn/interrupt`; no
se presenta como pausado hasta terminar o cerrar su proceso. La duración total cuenta
también esperas y pausas: una respuesta tardía no amplía automáticamente el presupuesto.

La cuota se consulta por `account/rateLimits/read` antes de cada intento, durante el turno
y al terminar para conservar una observación posterior (su ausencia queda explícita).
Datos ausentes/obsoletos, ventanas expiradas o un bucket desconocido bloquean. La vista
individual histórica corresponde al medidor `codex`; un `limitId` explícito se conserva
y la vista por buckets tiene prioridad. Nunca se selecciona el bucket con más saldo.
Una ventana secundaria ausente es válida. La duración y `resetsAt` son opcionales:
sin reset se limita la antigüedad por recepción; un reset presente pero pasado exige
refrescar. Ninguna ventana medida o un porcentaje ausente significa desconocido.
Se validan también los buckets que el SDK tipa como `Any`; no se convierten strings o
booleanos en porcentajes. Créditos y resets ofrecidos no autorizan ejecución.

Esta modalidad admite el medidor estándar `codex`; rechaza otros medidores y Spark
antes de inferencia. `model/list` 0.147.0 no publica un mapa general modelo/medidor y
Factory no inventa uno. El modelo/esfuerzo deben estar ofrecidos por el runtime. No se
inventan duraciones de ventana ni conversiones tokens/porcentaje. El uso del hilo procede
de `thread/tokenUsage/updated`; la cuota pertenece a la cuenta compartida con el resto
de Codex. Puede llegar con retraso: la reserva es margen, no garantía de no sobrepasar
un umbral. El límite de duración se aplica al proceso además de los intentos y tokens
observados; contar llamadas al SDK no limita sus turnos internos. No se compran créditos,
consumen resets, usan proveedores alternativos o escalan modelo/esfuerzo.

## Aceptación y arquitectura

El resultado queda en `factory/slice-<execution-id>` y `.factory/worktrees/<execution-id>`.
La rama/archivos del usuario no se cambian: no reset, clean, stash, push ni despliegue.
Una nueva slice parte del último commit aceptado por Factory. Se conserva el worktree
fallido para inspección; no hay borrado automático.

La publicación guarda una intención con tree y commit determinista, actualiza la ref con
compare-and-swap y registra el recibo SQLite. Un crash en cualquier frontera permite
reusar el mismo commit; no duplica implementación. El recibo liga arquitectura, plan,
definición, harness y código a evidencia PASS vigente. Cambiar archivos después no hace
que ese PASS autorice otra versión. No se modifica la revisión inmutable del roadmap ni
se marcan requisitos globales satisfechos.

Cambios en manifests de dependencias, SQL/migrations/schema y algunas nuevas importaciones
de persistencia, o una propuesta explícita del worker, conservan cambios y abren bloqueo
arquitectónico. La baseline no se reescribe y no se llama al arquitecto. Estas reglas no
detectan todos los cambios estructurales semánticos. Una respuesta humana no convierte
por sí sola una propuesta en una nueva baseline.

## Diagnóstico de cuota sin inferencia

`scripts/diagnose_execution.py --model MODEL --effort EFFORT --output informe.json`
consulta el SDK real dentro del aislamiento de Factory, sin abrir threads/turnos. Registra
versiones y hashes del ejecutable **incluido en el SDK** y del de terminal, inicialización,
autenticación efectiva (solo el tipo), configuración privada, cuota y causa sanitizada.
Nunca imprime contenido de auth.json, cuerpos de errores, credenciales ni identidad de cuenta.

`execution.diagnostic` y `next_action.next_step` conservan causas distintas: método no
disponible, autenticación/configuración, transporte, servicio, timeout, parsing,
datos insuficientes/obsoletos, reserva y agotamiento. Un RPC no reconocido conserva tipo
de excepción/código; no se reclasifica como saldo ilimitado. Status lee este diagnóstico
durable y no realiza consultas de red ni llamadas a modelos.

Se mantiene `openai-codex==0.147.0` y su runtime fijado, sin actualización global ni
sustitución del cliente. El SDK inicializa el protocolo y ofrece la petición JSON-RPC
tipada; `QuotaResponse` amplía únicamente el modelo de respuesta para permitir la vista
solo por buckets y validación estricta. `account/rateLimits/updated` está en el esquema
instalado y se recibe mediante `codex._client.next_notification()` (cola global, no la
cola del turno). Se usa lectura explícita para los controles; no hace falta provocar un
turno para conseguir notificaciones. Las respuestas no incluyen una fecha de medición
del backend: `observed_at` es la recepción local y no elimina posibles retrasos del servicio.

## Demo desechable y pruebas

Sin cuota, SDK simulado y comandos reales:

```bash
.venv/bin/python scripts/smoke_execution.py
.venv/bin/python -m unittest discover -v
```

El script anterior conserva un repositorio temporal con SDK simulado y muestra
FAIL → reparación → PASS. El escenario representativo del paso 8.1 usa código Python
existente, un helper de normalización y diez tests de aceptación preparados antes de
implementar. Su preparación de discovery/arquitectura/planning es sintética, validada y
publicada por los mecanismos habituales; nunca aporta la solución al worker.

Preparación sin inferencia, autorización de política por MCP y registro opcional para la app:

```bash
.venv/bin/python scripts/smoke_execution_real.py --register --model gpt-5.6-terra --effort low
```

Añadir `--run` ejecuta **un** smoke real por `factory_execution_policy` y `factory_execute`,
cierra el cliente MCP de lanzamiento y consulta el resultado con una nueva conexión.
El script nunca llama directamente al SDK de modelos. Límite: dos intentos en total,
180 segundos, 8.000 tokens observados, reserva predeterminada existente del 25% y una slice.
El límite de tokens usa telemetría observada: puede sobrepasarse antes de llegar un evento;
la duración se impone además al proceso. El modelo/esfuerzo se valida en cada runtime.
Cada invocación crea un producto desechable nuevo; no admite un producto existente.
La antigua demo `smoke_execution.py --prepare` conserva su política diferente (35%, un intento).

El paso 8.1 **sí completó un smoke con modelo real**, mediante MCP y proceso separado,
después de corregir el diagnóstico. La causa original fue el filtro externo de comandos
de la sesión de Codex App: `send()` devuelve EPERM y DNS falla; App Server devuelve
`-32603: failed to fetch codex rate limits: error sending request`. La misma consulta,
con aprobación para la conexión del controlador y sin cambiar el sandbox propio de
Factory, funciona. No hay una autoescalación en Factory: si su host bloquea red, se
conserva `quota_transport` y debe restaurarse la conectividad de ese proceso.

Resultado, límites de lo probado y ubicaciones: [informe del paso 8.1](execution-8.1.md).

Fuentes oficiales contrastadas con el SDK instalado:
[App Server: auth, cuota, uso e interrupción](https://learn.chatgpt.com/docs/app-server) y
[SDK Python y runtime incluido](https://learn.chatgpt.com/docs/codex-sdk) y
[sandbox de Codex](https://learn.chatgpt.com/docs/security). Los métodos de auth/modelos,
turnos e interrupción usan el SDK; la consulta tipada de cuota usa su cliente JSON-RPC.
