# Recuperación autorizada de una propuesta bloqueada

La segunda prueba real agotó la corrección normal y la recuperación automática de planning.
El proyecto conservaba llamadas y tokens, pero el controller no tenía un camino para
autorizar un esfuerzo adicional sobre esa propuesta. Esta corrección añade una concesión
explícita mediante `factory_execution_policy`, con el planner, crítico, gate, ledger y
proceso independiente existentes.

`planning_recovery` contiene `request_id`, `run_id`, `proposal_fingerprint` y `reason`.
La tool `factory_inspect` con `view=plan` proporciona la huella y el gate; `view=execution`
proporciona la definición de verificación y política ya fijadas. La autorización mantiene
esas definiciones exactas. No acepta otro modelo, esfuerzo, permisos ni unidades de trabajo.
Permite ampliar explícitamente llamadas, tokens y tiempo agregados con autorización previa;
registra los topes anteriores y nuevos sin reiniciar consumo. Suma la diferencia de tiempo
al deadline anterior; nunca lo recalcula como si el run fuera nuevo. Una configuración
ordinaria o reanudación no concede esa ampliación.

Cada concesión de operador corresponde a una propuesta concreta: exactamente una corrección
y una revisión independiente adicionales. Una propuesta revisada que mantenga objeciones
necesita otra autorización del operador; todas comparten el mismo presupuesto acumulado
y sus ampliaciones autorizadas. Cambiar el nombre de una petición sobre la misma propuesta no concede nada.
Los contadores de planning siguen aumentando desde
su valor actual, incluidos errores anteriores. El plan y sus criterios no se editan al
autorizar. Se conservan las llamadas, revisiones, fallos, arquitectura y fuentes previas.
Un requisito humano pendiente, fuentes obsoletas, consumo desconocido o presupuesto
insuficiente impiden conceder la recuperación. El gate sigue decidiendo la aceptación.

La petición es idempotente, incluso después del cierre: el mismo ID con otro contenido
se rechaza. Se conserva el historial de todas las concesiones y se reconocen también las
repeticiones de las antiguas. Una caída tras guardar la autorización
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

La revisión puede referirse a la raíz `execution_binding`, a IDs de sus checks y a códigos
de diagnósticos presentes en el contexto guardado de esa llamada, además de requisitos y
elementos del plan. Los códigos ausentes se rechazan, incluso si existen en otra propuesta.
Reconocer esas referencias conserva los hallazgos;
no los convierte en aprobación. Si el modelo terminó y falló el guardado/validación del
checkpoint, planning reutiliza la respuesta del ledger de análisis con el mismo contexto,
esquema, instrucciones, fuentes y autorizaciones. No repite inferencia ni incrementa
contadores; los errores anteriores siguen registrados. Esto también funciona si esa
respuesta consumió la última llamada de fase.

El contexto de planning también explicita la evidencia que ya exige el controller:
`requirement_acceptance.milestones` son recibos de cierre obligatorios, no etiquetas.
`MilestoneGate.publish` exige aceptaciones de todas las slices; `ProjectGate.obligations`
exige las milestones contribuyentes cerradas, además de las comprobaciones finales del
producto. El recibo final conserva sus referencias en `binding.milestones`, y los recibos
de milestone conservan ejecuciones y commits de sus slices. Esto acredita orden/cierre
de la entrega; no sustituye el comportamiento integrado ni el recorrido principal.
La regresión comprueba las referencias reales y que retirar un cierre vuelve a bloquear
la aceptación. No se añade otro runner ni un test del producto que lea estado de Factory.

Cada llamada de planning recibe el gate determinista calculado sobre su propuesta actual.
El crítico no hereda los errores guardados de una propuesta anterior a la corrección.
Los IDs originales de verificaciones siguen siendo obligatorios; el diagnóstico enumera
los que faltan y las referencias de entrada conservan su vinculación a `project_close`.

Una comprobación `project_close` puede acreditar un requisito de una milestone anterior:
se ejecuta sobre el producto integrado después de todos los cierres. La validación del
contrato ya no exige añadir como contribuyente la milestone usada para situar ese gate
en el roadmap. Los cierres de las contribuyentes originales siguen siendo obligatorios;
un check de cierre de otra milestone no obtiene esa excepción. Los diagnósticos identifican
todos los contratos de requisito inválidos para que la corrección reciba causas concretas.
También identifican target, índice y texto original de cada criterio sin comprobación,
incluidas las condiciones de cierre que siguen a los success criteria de una milestone.
El planner debe justificar la correspondencia con la evidencia; el diagnóstico no asigna
checks ni cambia el plan automáticamente.

El contexto describe también el perfil de copia limpia ya existente: Python de sistema,
allowlist de biblioteca estándar/runtime, red ausente, producto de solo lectura y `/tmp`
y `/home` temporales vacíos. El PASS de los recorridos acredita independencia de estado
previo y dependencias ausentes. No promete ausencia universal de operaciones de archivos
o bases de datos en rutas no ejercitadas; los temporales efímeros siguen siendo posibles.

## Evidencia adicional antes de aceptar el plan

Si se necesita una comprobación adicional para una condición original, el operador puede
autorizar `planning_recovery.verification_extension=true` con su definición concreta.
Solo añade checks y recursos inmutables versionados, fuera de permisos de escritura del
worker. Conserva íntegros los checks, recursos, condiciones, exclusiones y entrega originales.
La definición recibe otro ID y la anterior queda intacta; el historial conserva la relación.
El planner debe vincular la nueva evidencia y el crítico revisarla. No puede usarse después
de aceptar el plan o implementar producto. La extensión no responde decisiones humanas:
si alguna está pendiente, no arranca inferencia hasta registrar su respuesta real.

`test_no_residual_storage.py` ejecuta los seis ejemplos CLI ya declarados con eventos de
auditoría Python. Detecta escrituras, SQLite y lanzamiento de otros procesos, incluidos
intentos cuyo error captura el producto. Usa el mismo runner limpio. La regresión ejecuta
productos desechables con y sin esos efectos; no implementa el piloto real. Su garantía
cubre esos recorridos Python, no código nativo arbitrario ni todos los inputs posibles.
