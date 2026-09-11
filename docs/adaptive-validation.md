# Validación del flujo adaptativo

El cambio habilita continuidad con herramientas normales de Codex, sin trasladar a los proyectos
nuevos las condiciones fijas del piloto Python. La aceptación de arquitectura no requiere un
acto formal. Las comprobaciones del producto las elige Codex y las revisiones quedan explicadas.

Las pruebas específicas usan productos desechables separados y modelos simulados. Cubren
conversación, cambios de foco, checks opcionales, fallo real de una entrada y corrección,
mensajes durante ejecución, pausa, recuperación de resultados/turnos, consumo acumulado,
reparación del checkpoint, revisión de criterios, CLI y conservación de proyectos históricos.
Una prueba usa transporte MCP STDIO real: cierra el cliente mientras trabaja un modelo
simulado y comprueba que los siguientes pasos y la entrega terminan en el proceso desacoplado.

Además se hizo una única prueba pequeña con `gpt-5.6-terra/low`, en un repositorio desechable,
creando una utilidad JavaScript para Node sin dependencias. Se usaron herramientas reales de
terminal y edición. El primer turno se interrumpió al alcanzar el límite local de prueba de
100.000 tokens; se amplió a 500.000 y se recuperó la misma instancia, conservando archivos,
run y consumo. Terminó en dos turnos SDK y 207.961 tokens totales reportados, en aproximadamente
76 segundos de ejecución de turnos. Un turno SDK puede incluir varias peticiones al modelo.
No se infiere un ahorro de coste a partir de esos totales, que incluyen entrada reutilizada.

Tras completed se ejecutaron cuatro comprobaciones locales independientes: suma positiva,
decimales/negativos, argumento no numérico y argumentos ausentes. Pasaron. Repetir resume no
inició inferencia. La [evidencia](evidence/adaptive-runtime-smoke.json) conserva comandos,
resultados, hashes de archivos y estado final. El producto de esa prueba está en una ruta
`/tmp` desechable; no es una entrega permanente del usuario.

El plugin instalado se comprobó mediante su launcher MCP real: ofrece configuración adaptativa
y sigue devolviendo `project_verified` para el piloto histórico `p_ba973102351c9eac`, sin
iniciar ni reanudar su ejecución. Esta prueba es de MCP; no es una comprobación humana del flujo
nuevo desde la interfaz de Codex App. La comprobación humana anterior pertenece al piloto
verificado y se conserva como tal.

Quedan como límites funcionales: el selector temporal de modelo/razonamiento del chat no llega
por MCP; las necesidades de herramientas, dependencias, cuentas o servicios dependen del
proyecto y del entorno; no hay notificaciones espontáneas a una conversación cerrada ni arranque
automático tras reiniciar el host. completed es la conclusión de Codex, no un recibo independiente
sobre un commit limpio. Esta prueba pequeña no demuestra que cualquier aplicación o integración
externa esté validada.

La suite completa pasó: 356 tests en 524,871 segundos, sin fallos ni skips. Tras los últimos
ajustes de inicialización CLI y presentación de estado, pasaron 15 tests específicos en
5,792 segundos (incluyen los 14 adaptativos de la suite y un caso CLI adicional). Los recuentos
se solapan. [Resultados y logs](evidence/adaptive-validation.json).
