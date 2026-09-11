# Evidencia de la validación final (paso 11)

La implementación se realizó sobre `7dcd8f8`, recuperado mediante pull fast-forward. Factory
no se utilizó para desarrollarse recursivamente: las ejecuciones de producto de la suite
usan repositorios temporales separados.

## Resultado reproducible de desarrollo

La suite completa termina con **294 tests PASS en 419,104 segundos**, sin errores, fallos
ni skips, mediante `.venv/bin/python -m unittest discover -v`. Se conserva su
[log completo](evidence/final-validation-11-tests.txt) y un
[manifiesto con hashes de los archivos probados](evidence/final-validation-11-checks.json),
incluidos los archivos nuevos aún sin commit. La rama continúa siendo `main`; este paso
no crea commits de Factory, hace push, publica paquetes ni despliega.

También pasan `compileall` sobre `factory`, `tests` y `scripts`, `git diff --check`, los
validadores de plugin y skill, y la ayuda de `validate-project`. La revisión corrigió el
tratamiento de procesos muertos por timeout, limitó los recursos nativos del perfil limpio
y comprobó que los comandos del informe conservan el directorio de trabajo de sus fixtures.

## Qué demuestran las pruebas

El escenario existente de dos milestones se amplía en `tests/project_fakes.py` y
`tests/test_project_validation.py`. Ambos milestones obtienen recibos; la CLI acepta sus
inputs positivos locales pero falla al ejecutar la entrada integrada con signo desde una
copia limpia. Factory conserva el rechazo, crea una unidad de remediación, acepta otro commit,
repite las verificaciones reales y solo entonces publica `project_verified` y entrega.

Otros casos cubren residuos no versionados y dependencia de `.git`; ausencia de `.venv`,
estado interno y secretos heredados; requisito transversal sin aceptación completa;
capacidad/dependencia no disponible; integración simulada; aprobación humana que deja de
valer tras una corrección; modificación de código/ref antes de publicar; criterios congelados;
agotamiento global ante un segundo fallo real; pausa y límites agregados; recuperación desde
el archivo de un check, desde la evidencia verificada, tras rollback y tras recibo sin informe;
solicitudes repetidas; comandos de entrega ejecutables y referencia remota sin cambios.

El repaso abarca el contrato de discovery y su vínculo a arquitectura/planning, los journals
y presupuestos de ejecución/continuidad, el runner y sus recursos, los cierres Git/SQLite,
las consultas de requisitos y la interfaz service/MCP. Las mejoras futuras documentadas se
basan en límites observados; no se añade una auditoría universal del producto ni otro motor.

## Transporte MCP real, modelos simulados

`tests/test_milestone_mcp.py` conserva el escenario de desconexión existente y lo lleva hasta
el cierre final. Usa el SDK MCP 2.2.0, conexiones stdio y un controller en proceso separado.
El cliente se desconecta antes de terminar la primera implementación. Otra conexión observa
los dos recibos de milestone, el rechazo final, la corrección, el recibo de proyecto y la
entrega. Las peticiones repetidas mantienen el mismo run. La implementación y el refinamiento
usan FakeSDK; los commits, aislamiento, tests Python, CLI y transporte MCP son reales.

Esto no demuestra una ejecución con modelo real ni una interacción efectivamente realizada
desde la UI de Codex App. No se declara validada en uso real la Factory.

## Cuota real y mismo smoke pendiente

El [diagnóstico sanitizado](evidence/final-validation-11-quota.json) usa el SDK/runtime fijado
0.147.0, autenticación ChatGPT y `account/rateLimits/read`, sin crear turnos de modelo.
Observa 85 % usado en el medidor `codex`, reserva del 25 % y bloqueo `quota_reserve`.
No se reduce la reserva, cambia de proveedor/medidor o utiliza facturación alternativa.
El diagnóstico registra **cero llamadas de inferencia**.

Se amplían `scripts/smoke_continuation.py` y su fixture existente hasta la validación final
del mismo producto de resumen/ranking de registros. La entrada pública es su API Python;
se reutilizan los tests y ejemplos aprobados, sin añadir una CLI ajena a su alcance. La
política conserva dos unidades, cuatro llamadas, 300 segundos, 30.000 tokens y reserva del
25 %. Añade autorización final determinista y deja la remediación automática deshabilitada.

La ruta del piloto previamente preparado, `/tmp/factory-continuation-smoke-bc7lwhrv`, ya no
existe en este host. No se ha reconstruido su identidad ni creado una demo alternativa. Para
reanudar de verdad hace falta recuperar ese repositorio y su estado en el host original,
y que la cuota respete la reserva. El driver rechaza cambiar fuentes de un piloto intentado.
El test de preparación comprueba el contrato extendido sin inferencia ni reinicio de límites.

Permanecen pendientes el recorrido completo con modelo real y su comprobación desde Codex
App. La skill y las tools documentan «Valida el proyecto terminado y prepara su entrega local»;
pasar tests de transporte no sustituye esa comprobación de UI.
