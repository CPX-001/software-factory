# Continuidad adaptativa

El flujo conserva `Store`, `FactoryService`, MCP, `Runtime` y el controlador desacoplado.
`CodexAdaptive` reutiliza streaming, interrupción y recuperación de `CodexExecution`; cambia
las herramientas disponibles y el checkpoint. Usa el SDK local `openai-codex` y su
[App Server](https://learn.chatgpt.com/docs/app-server).

El worker mantiene las instrucciones base de Codex y añade continuidad en `developer_instructions`.
Trabaja directamente en el repositorio con herramientas, plugins y skills normales. Se desactiva
la interfaz Factory para impedir recursión. Usa acceso completo, sin aprobaciones de comandos:
es el uso local solicitado, no un aislamiento de productos hostiles.

Cada paso recibe mensajes nuevos, memoria compacta, referencias a documentos, tareas y el siguiente
trabajo propuesto. Codex decide cambiar de foco o volver atrás. Su respuesta estructurada describe
estado; edita con herramientas normales, sin propuestas cerradas de archivos. La memoria admite
12.000 caracteres; los detalles van en documentos del producto. Cada paso abre un hilo nuevo con
ese contexto, evitando reenviar todo el historial. No se promete un ahorro medido de tokens.

Las tablas `adaptive_project`, `adaptive_inputs`, `adaptive_steps` y `adaptive_questions` añaden
estado al SQLite existente. `factory_runs` y sus locks siguen siendo propietarios del proceso.
Publicar un checkpoint consume solo sus entradas; las posteriores reciben otro paso aunque el
anterior se haya declarado completado. La decisión de detenerse se serializa con nuevos mensajes.

Codex elige los checks, que pueden estar vacíos. No se puede declarar completed con tareas,
preguntas o checks obligatorios pendientes. Un check elegido no desaparece silenciosamente:
las revisiones registran su razón y conservan la definición anterior en el historial. Esto permite
revisar una solución sin aprobación formal. Factory no certifica independientemente esas afirmaciones.

La respuesta se guarda antes de aplicar el checkpoint. La recuperación reutiliza esa respuesta o
consulta el turno exacto en Codex. Un turno incompleto puede haber dejado ediciones: el siguiente
recibe esa advertencia y debe inspeccionarlas. No se arranca otro worker mientras el proceso anterior
identificado siga vivo. La pausa interrumpe y conserva consumo. Transporte, autenticación y límites
producen bloqueos recuperables; los fallos concretos del producto pueden corregirse durante el flujo.
No se instala recuperación automática al arrancar la máquina.

Los límites de llamadas, tokens y tiempo son opcionales y acumulativos. Este modo no impone la reserva
del piloto; los errores reales de cuenta siguen bloqueando. Se conserva la configuración y login
Codex existentes, sin facturación alternativa. La selección temporal de la App no llega por MCP.
`calls` cuenta turnos SDK, no peticiones individuales al modelo: un turno normal puede hacer varias
interacciones con herramientas. Los tokens son los totales reportados por el SDK, incluidos los de
entrada reutilizada; no representan por sí solos coste facturado ni ahorro de caché.

El informe es una proyección determinista fuera del código. Conserva alcance, checks, documentos,
diferidos y Git HEAD/estado observados; el generador no crea commits, paquetes ni despliegues.
Un mensaje nuevo puede revisar el proyecto, conservando checkpoints e informes previos. Resume
sin trabajo nuevo recupera un informe ausente y devuelve el resultado existente sin inferencia.

Este modo se aplica únicamente al inicializar proyectos nuevos. Repetir el registro no convierte
proyectos históricos. Sus tests seleccionan `verified` explícitamente sin rebajar las condiciones.
