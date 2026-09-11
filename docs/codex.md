# Usar Software Factory desde Codex

La interfaz principal es el plugin local **Software Factory**: una skill corta y diez
MCP tools por STDIO. CLI y MCP usan `FactoryService`; el estado, los gates y el controller
pertenecen a Factory. Planning produce un roadmap progresivo; la ejecución de producto se habilita explícitamente con presupuesto, permisos y checks versionados. La autorización original permite una slice; el [piloto multislice](continuation.md) requiere una política adicional explícita. Consulta [ejecución y recuperación](execution.md).

## Instalar en este host

Desde el checkout, con el login Codex/ChatGPT existente:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/install_codex_plugin.py --allow-root /ruta/a/tus/proyectos
```

El instalador usa la skill `plugin-creator` instalada, valida el paquete y lo registra en
el marketplace personal como `software-factory@personal` (o el nombre personal existente).
Reescribe el launcher con rutas absolutas a este checkout y su `.venv`. Mantén esas rutas;
si las cambias, repite la instalación. No publica nada ni inicia workers.

Abre una conversación nueva en Codex App usando **el mismo host/entorno**. El plugin usa
el filesystem local de ese host: una instalación Linux/WSL no instala un ejecutable Windows.
Si no aparece el marketplace personal, reinicia la aplicación. Puedes invocar
`$software-factory` o seleccionar el plugin en el compositor.

Ejemplos de mensajes:

- «Usa Software Factory. Crea un proyecto en /ruta/a/tus/proyectos/oportunidades para
  analizar repositorios GitHub y encontrar oportunidades SaaS».
- «Quiero que sea para desarrolladores individuales primero».
- «¿Cómo va el proyecto? ¿Qué decisiones necesitas de mí?».
- «Enséñame la arquitectura vigente», «Pausa el proyecto» o «Continúa».
- «¿Cuál es el plan?», «¿Qué milestones hay?», «¿Cuál es la próxima slice?».
- «¿Qué requisitos siguen pendientes?», «¿Dónde están los gates importantes?».
- «Valida el proyecto terminado y prepara su entrega local».

Estas consultas usan las vistas `plan`, `milestones`, `next_slice`, `requirements` y
`verification` de `factory_inspect`; `factory_decisions` muestra las decisiones humanas.
Consulta [el contrato de planning](planning.md).

El path se pide solo al registrar. Después se conserva la selección en
`~/.local/state/software-factory/registry.sqlite3` (`FACTORY_HOME` permite otro directorio).
Las tools aceptan IDs registrados; inicializar exige estar dentro de raíces autorizadas
localmente. Codex conserva el ID devuelto para fijar el proyecto de su conversación.
Sin selección y con varios proyectos se pide elegir. No se interpreta el cwd del servidor
como el workspace de la conversación. Selección explícita e inicialización son las únicas
operaciones que cambian el proyecto activo.

## Tools y ejecución

`factory_project` (list/init/select), `factory_status`, `factory_message`,
`factory_decisions`, `factory_answer`, `factory_inspect`, `factory_pause`, `factory_resume`.
`factory_execution_policy` autoriza la política y definición tipada; `factory_execute`
selecciona la próxima slice preparada y devuelve su ID sin esperar a que termine.
Con `action=validate_project`, esa misma tool autoriza la validación final y entrega local
del proyecto cerrado, conservando el run y sus límites. La remediación automática exige
`automatic_remediation=true` por separado. Para incluir todo el recorrido desde milestones,
la política puede autorizar `final_validation` antes del run. La vista `project_validation`
muestra commit, criterios pendientes, checks, exclusiones, bloqueos y entrega; distingue
`project_ready_for_validation`, `project_validating`, `project_verified`, `delivery_pending`
y cambios posteriores en `version_pending`. Consulta [el contrato final](project-validation.md).
Las respuestas usan `{ok, data}` o `{ok:false, error:{code,message,details}}`; los errores
MCP también llevan `isError`. Status es compacto; inspect pide documentos completos.

Message y answer guardan la entrada y arrancan trabajo elegible sin esperar a su resultado.
Resume es una orden de continuación, no una fase: procesa discovery,
architecture y planning; también recupera una ejecución previamente autorizada. Se detiene por preguntas/decisiones, bloqueo, fallo,
límite o pausa. Un fallo no se reintenta automáticamente. Los checkpoints sobreviven a
reinicios; `request_id` permite reintentar un mensaje de discovery sin duplicarlo.

El proceso de Factory se separa de MCP y no necesita una conversación abierta. En las fases
de análisis pause es cooperativo. En ejecución solicita interrupción del runtime y conserva
`pause_requested` hasta que se detenga el turno/proceso; no empieza otro intento.
Si muere el worker o se reinicia la máquina, el estado sobrevive; usa resume para recuperarlo.
No es un daemon que se relance automáticamente al arrancar el sistema.

La skill presenta y envía información; no repite el razonamiento de los workers. Sus hilos
Codex desactivan la interfaz Factory para impedir recursión. Las tools no exponen shell,
SQL, cambios directos de fase, gates o revisiones arquitectónicas.

## Recuperación

Los comandos CLI anteriores siguen disponibles. `pause`, `resume` y `allow-root` son nuevos.
La CLI puede inspeccionar estado completo y ejecutar discovery/architecture/planning de forma síncrona;
usa el mismo service y los mismos locks. Para comprobar la integración y la suite sin cuota:

```bash
codex plugin list --marketplace personal --json
.venv/bin/python -m unittest discover -s tests -v
```

Fuentes verificadas: [MCP en Codex](https://developers.openai.com/codex/mcp),
[plugins locales y formato compatible](https://developers.openai.com/plugins/build/plugins),
[SDK oficial de MCP](https://github.com/modelcontextprotocol/python-sdk).
Se usa el formato `.codex-plugin/plugin.json` compatible con el Codex local 0.149.1,
MCP SDK 2.2.0 y el SDK Codex 0.147.0 existente.
