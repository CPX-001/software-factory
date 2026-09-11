# Mejoras justificadas por la revisión del paso 11

No forman parte de esta implementación ni amplían sus permisos. La prioridad es cerrar las
validaciones de uso pendientes antes de aumentar el alcance técnico.

## 1. Conservar el piloto pendiente en una ubicación durable

Las evidencias de los pasos 9/10 sitúan el único piloto en
`/tmp/factory-continuation-smoke-bc7lwhrv`. Esa ruta ya no existe en este checkout/host.
El código y los informes versionados sobreviven; su repositorio desechable y SQLite no.
Conviene ubicar futuros pilotos reanudables bajo un directorio local persistente con una
política explícita de retención. La migración de un piloto debe conservar identidad,
fuentes, autorización, recibos y contadores; reconstruir una fixture no prueba recuperación.

## 2. Autorizar ampliaciones de presupuesto sin ocultar consumo previo

La entrada en validación final conserva el deadline original. Es correcto que una autorización
tardía no renueve tiempo ni llamadas, pero hoy un run agotado necesita una intervención fuera
del recorrido automático para disponer de más presupuesto. Una futura operación explícita
podría añadir una concesión auditada, mostrando presupuesto inicial, ampliación y consumo
acumulado. No debería reiniciar contadores ni el ciclo global de remediación final.

## 3. Aprovisionamiento Python offline con dependencias fijadas

El perfil limpio solo soporta biblioteca estándar. Un producto que necesita una dependencia
Python legítima queda bloqueado aunque funcione en la máquina del desarrollador. Una ampliación
útil sería consumir wheels previamente autorizadas, con hashes y procedencia, en un entorno
nuevo sin red. Requiere un contrato de recursos y pruebas de aislamiento; no basta con heredar
`.venv` o ejecutar pip arbitrariamente.

## 4. Invalidación más precisa cuando el coste lo justifique

Actualmente cualquier cambio de commit o de entorno relevante invalida la evidencia final.
Es conservador y apropiado para este piloto pequeño. Si el coste de gates crece, podrían
declararse dependencias de cada check sobre archivos, contratos y configuración para reutilizar
solo resultados cuyo ámbito siga idéntico. Harían falta pruebas de cambios transitivos y de
configuración; un simple diff de archivos directos no sería suficiente.

## Validación pendiente que tiene prioridad

Completar el mismo escenario de dos milestones con modelo real y verificar su invocación desde
Codex App. Los tests con modelos simulados y transporte MCP real no lo sustituyen. El diagnóstico
histórico del paso 11 conserva `quota_reserve`. En el paso 12, una autorización explícita retiró
la reserva para el único piloto y se completaron discovery, arquitectura y planning con modelo
real. El [recorrido actual](end-to-end-acceptance.md) está pendiente de vincular la verificación
del plan antes de implementar; la aceptación del producto continúa sin ejecutar.
No propongo en este paso nuevos lenguajes,
paralelismo, despliegue ni resolución automática de propuestas arquitectónicas.
