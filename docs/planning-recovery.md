# Recuperación autorizada de una propuesta bloqueada

La segunda prueba real agotó la corrección normal y la recuperación automática de planning.
El proyecto conservaba llamadas y tokens, pero el controller no tenía un camino para
autorizar un esfuerzo adicional sobre esa propuesta. Esta corrección añade una concesión
explícita mediante `factory_execution_policy`, con el planner, crítico, gate, ledger y
proceso independiente existentes.

`planning_recovery` contiene `request_id`, `run_id`, `proposal_fingerprint` y `reason`.
La tool `factory_inspect` con `view=plan` proporciona la huella y el gate; `view=execution`
proporciona la definición de verificación y política ya fijadas. La autorización mantiene
esas definiciones exactas. No acepta otro modelo, esfuerzo, permisos, número de llamadas,
tokens ni unidades de trabajo. Solo permite ampliar explícitamente el tiempo agregado,
sumando la diferencia al deadline anterior; nunca lo recalcula como si el run fuera nuevo.

Hay una sola concesión de operador por run de análisis: exactamente una corrección y una
revisión independiente adicionales. Los contadores de planning siguen aumentando desde
su valor actual, incluidos errores anteriores. El plan y sus criterios no se editan al
autorizar. Se conservan las llamadas, revisiones, fallos, arquitectura y fuentes previas.
Un requisito humano pendiente, fuentes obsoletas, consumo desconocido o presupuesto
insuficiente impiden conceder la recuperación. El gate sigue decidiendo la aceptación.

La petición es idempotente, incluso después del cierre: el mismo ID con otro contenido
se rechaza. Otro nombre no abre una concesión nueva. Una caída tras guardar la autorización
y antes de arrancar se recupera con la misma petición. La publicación utiliza los locks
de proceso y lanzamiento y comprueba revisión y propietario actuales.

Una autorización válida continúa el mismo run automáticamente. Una pausa guardada o
recibida antes del arranque conserva su efecto; autorizar la corrección no la levanta.
Cerrar el cliente MCP no es una pausa. No se aplica esta concesión a implementaciones,
recibos o validaciones finales: su remediación global conserva el límite original.

Las regresiones reutilizan repositorios desechables y modelos simulados. Incluyen un
rechazo de esquema anterior, contador de fase superior al límite inicial, conservación
del historial, fuentes y checks inmutables, reintentos, pausa concurrente y recuperación
tras caída. El escenario existente de dos milestones también recorre mediante MCP la
autorización, desconexión, corrección, revisión, implementación y entrega verificada.
Esto no sustituye la evidencia del recorrido con modelo real.
