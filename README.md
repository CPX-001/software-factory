# Software Factory

Continuidad persistente para desarrollar proyectos con Codex: conversar sobre una idea,
investigar, concretar el producto, diseñar, implementar y comprobarlo sin dirigir manualmente
cada paso. Codex elige el proceso según el proyecto y utiliza sus herramientas y skills normales.
Factory conserva memoria compacta, entradas, preguntas, tareas y puntos de continuidad.

Los proyectos nuevos usan un flujo **adaptativo**. Las fases indican el foco actual, pueden
revisitarse y no exigen aprobación formal de arquitectura. No hay lenguaje, framework ni suite
de tests impuestos. Codex elige las comprobaciones apropiadas y continúa hasta completar el
alcance o necesitar una respuesta real del usuario. Las mejoras futuras quedan diferidas.
Consulta [cómo funciona](docs/adaptive-workflow.md).

La interfaz principal es el plugin de Codex App. En una conversación nueva:

> Usa Software Factory. Trabajemos en /ruta/a/mi-proyecto. Mi idea es […]. Ayúdame a concretarla
> y desarrolla lo acordado; pregúntame cuando una decisión necesite mi criterio.

No hace falta decir «continúa» entre pasos. Puedes conversar, cambiar decisiones, consultar o
pausar. El proceso continúa aunque se cierre la conversación. [Instalación y uso](docs/codex.md).

La CLI usa el mismo servicio:

```bash
.venv/bin/python -m factory init /ruta/a/mi-proyecto
.venv/bin/python -m factory message /ruta/a/mi-proyecto --message "Quiero desarrollar…" --request-id idea-1
.venv/bin/python -m factory status /ruta/a/mi-proyecto
.venv/bin/python -m factory process /ruta/a/mi-proyecto
.venv/bin/python -m factory pause /ruta/a/mi-proyecto
.venv/bin/python -m factory resume /ruta/a/mi-proyecto
```

El estado vive en `PROYECTO/.factory/state.sqlite3`. Codex mantiene los documentos del producto.
La entrega genera `.factory/deliveries/checkpoint-N/REPORT.md`. `completed` expresa la conclusión
de Codex sobre el alcance y los checks reportados; no es una certificación independiente.

El worker hereda la configuración de Codex del host; MCP no recibe el selector temporal del
chat. Se pueden fijar modelo, razonamiento, velocidad o límites acumulados por proyecto.
La memoria compacta evita reenviar todo el historial de Factory, sin prometer un ahorro fijo.

Los pilotos anteriores mantienen el workflow `verified`, sus contratos y recibos. Su
[validación final](docs/project-validation.md) sigue limitada al entorno Python declarado;
la [entrega real verificada](docs/automatic-pilot-result.md) conserva su evidencia. Esos contratos
no se imponen a los proyectos adaptativos ni demuestran soporte probado de otros servicios.

La suite normal usa modelos simulados y productos desechables separados:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Para desarrollar Factory se usa Codex directamente, sin ejecutarla recursivamente sobre sí misma.

[Pruebas realizadas y límites comprobados](docs/adaptive-validation.md).
