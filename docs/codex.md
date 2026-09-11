# Usar Software Factory desde Codex

Puedes pasarle a Codex la URL del repositorio y pedirle que instale el plugin:

> Instala Software Factory desde https://github.com/CPX-001/software-factory
> siguiendo su README.

La instalación nativa desde Git usa el marketplace incluido en el repositorio:

```bash
codex plugin marketplace add https://github.com/CPX-001/software-factory.git
codex plugin add software-factory@software-factory-git --json
```

Si el repositorio es privado, inicia sesión en GitHub en ese ordenador y configura
el acceso Git (por ejemplo, `gh auth login` y `gh auth setup-git`, o una clave SSH).
No hace falta cambiar la visibilidad del repositorio.

Usa la ruta `installedPath` que devuelve el segundo comando para ejecutar
`python3 <installedPath>/scripts/plugin_mcp.py --prepare`. El agente puede completar
estos pasos al recibir la URL; no hace falta que el usuario gestione un clon.
La preparación descarga el motor y sus dependencias sin llamadas al modelo. No se
publica en el directorio de OpenAI ni requiere conservar una copia de desarrollo.
Para actualizar desde Git:

```bash
codex plugin marketplace upgrade software-factory-git
codex plugin add software-factory@software-factory-git
```

Prepara también la nueva versión con `scripts/plugin_mcp.py --prepare` en su ruta
instalada antes de abrir otro chat. El arranque MCP también prepara el entorno si
falta, pero una descarga lenta puede superar el timeout de Codex; la preparación
explícita durante la instalación evita ese problema.

Las versiones publicadas en Git deben incrementar `version` en `plugin.json` y
`.codex-plugin/plugin.json`, para que Codex renueve la copia instalada. Si ya usabas
el instalador personal, evita activar dos copias del mismo plugin: después de
comprobar la instalación de Git, puedes desinstalar la anterior con
`codex plugin remove software-factory@personal` (sustituye `personal` por el nombre
real de tu catálogo si lo cambiaste). Eso no borra la memoria de los proyectos.

Como alternativa, puedes generar un [paquete privado ZIP](private-plugin.md):

```bash
python3 scripts/build_codex_plugin.py
```

Copia y descomprime el ZIP de `dist/` y ejecuta `python3 install.py` en la carpeta
extraída. Si ya tienes este checkout, basta con `python3 scripts/install_codex_plugin.py`.
Requiere Python 3.11+ y Git; el instalador prepara su propio entorno y descarga las
dependencias. Ya no requiere una `.venv` del repositorio ni la skill `plugin-creator`.

El instalador usa el marketplace personal y el login Codex/ChatGPT existente. No inicia
inferencia ni publica el plugin. El launcher referencia una copia instalada del motor en
`~/.local/share/software-factory/runtimes/`; puedes borrar el ZIP y la copia de desarrollo.
Abre un chat nuevo de Codex App en el mismo host para cargar la skill y tools actualizadas.

Empieza con una idea y una ubicación:

> Usa Software Factory. Crea mi proyecto en /ruta/a/proyectos/agenda. Quiero una agenda para
> […]. Concretemos el producto y desarrolla lo acordado sin pedirme continuar entre pasos.

Los proyectos nuevos usan `adaptive`: Codex decide cómo investigar, diseñar, implementar y
comprobar cada producto con herramientas y skills instaladas. Arquitectura, alcance y documentos
pueden revisarse durante el trabajo. No hay aprobación formal obligatoria, secuencia fija de
fases ni tests preimpuestos. Las preguntas necesarias se presentan en la conversación. Las
entradas durante una ejecución se incorporan en el siguiente punto de continuidad.

Puedes pedir «¿Cómo va?», «Revisa esta decisión», «Enséñame el plan», «Pausa» o «Reanuda».
Cerrar MCP o la App no cancela el proceso. Tras reiniciar la máquina hay que reanudar: no se
instala un servicio de arranque. Las novedades en segundo plano se consultan por estado;
MCP no entrega mensajes espontáneos en una conversación cerrada.

`factory_project` inicializa, selecciona, lista o configura. `factory_message` envía entradas;
`factory_answer` responde una pregunta identificada. `factory_status` consulta sin inferencia.
`factory_inspect process` muestra la memoria; `plan`, tareas; `verification`, checks;
`execution`, pasos y observaciones del runtime. `factory_pause` y `factory_resume` controlan
el mismo proceso. El flujo adaptativo no requiere una política de ejecución previa.

Modelo y razonamiento heredan la configuración Codex del host. El selector temporal del chat
no se transmite automáticamente. Para fijarlos por proyecto, `factory_project` acepta:

```json
{"action":"configure","project":"p_0123456789abcdef","settings":{"model":"gpt-5.6-terra","effort":"low"}}
```

Son ajustes para los siguientes pasos. Null restaura herencia. `service_tier` configura velocidad
sin cambiar proveedor ni facturación. Los límites opcionales `max_calls`, `max_tokens`,
`max_seconds` acumulan consumo en el proyecto, también tras pausas/reinicios. No establecer
límites adicionales no elimina los límites de la cuenta Codex.
`max_calls` cuenta turnos SDK; cada turno puede incluir varias interacciones con herramientas/modelo.

Al terminar, `completed` y el informe local describen alcance y comprobaciones reportadas por
Codex con la versión Git observada. Puede haber archivos sin commit. No equivale a
`project_verified`, despliegue o auditoría de seguridad.

Los proyectos antiguos conservan sus contratos: [guía del workflow verificado](verified-codex.md).
`--allow-root` sigue disponible al instalar para registrar proyectos nuevos de ese tipo.
