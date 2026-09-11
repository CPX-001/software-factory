# Usar Software Factory desde Codex

Instala el plugin local desde este checkout y su entorno Python:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/install_codex_plugin.py
```

El instalador usa el marketplace personal y el login Codex/ChatGPT existente. No inicia
inferencia. El launcher referencia este checkout y `.venv`; conserva esas rutas. Abre un chat
nuevo de Codex App en el mismo host para cargar la skill y tools actualizadas.

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
