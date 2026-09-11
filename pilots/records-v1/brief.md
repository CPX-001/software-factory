# Datos del piloto: resumen y ranking de registros

Soy un desarrollador que necesita revisar pequeños conjuntos de registros sintéticos en
su máquina, sin servicios externos. Quiero una utilidad Python de biblioteca estándar,
usable desde Python y desde terminal. Este texto y los ejemplos son datos de prueba;
no son respuestas de una persona distinta ni una arquitectura o un plan aprobados.

El repositorio contiene `record_rules.py`, un helper existente cuya normalización
`strip().casefold()` debe conservarse, y tests independientes preparados antes de implementar.
Faltan `records.py` y `category_report.py`: Factory debe desarrollarlos.

- `records.summarize(records)` recibe una lista de diccionarios con `category` y `amount`.
  Admite claves adicionales y no modifica la entrada. La categoría es un string no vacío
  tras quitar espacios. El importe es int/float finito, no negativo y nunca bool.
  Una entrada incorrecta lanza ValueError. Devuelve count, total y by_category, con count/total
  por categoría normalizada. Una lista vacía devuelve 0, 0 y un diccionario vacío.
- `category_report.rank_categories(records)` utiliza la API anterior y devuelve objetos
  category/count/total ordenados por total descendente y categoría ascendente para desempatar.
  Conserva validación y ausencia de mutaciones; vacío produce una lista vacía.
- `python category_report.py examples/valid.json` carga una lista JSON y escribe el ranking
  como JSON compacto, claves ordenadas, escape ASCII y un salto de línea final. Sin mensajes
  adicionales en stdout. Un archivo inválido termina con código 2 y diagnóstico en stderr;
  no presenta una salida parcial. No necesita una base de datos ni archivos residuales.

Ejemplo: Books=7, Food=10 y FOOD=2 produce
`[{"category":"food","count":2,"total":12},{"category":"books","count":1,"total":7}]`.
Los empates A=1, z=1 colocan a antes de z. Se comprueban cero, vacío, Unicode/casefold,
NaN/infinito, importes negativos/bool, campos ausentes y datos de tipo incorrecto.

Propongo dos hitos por razones de producto: primero, un resumen validado que pueda reutilizarse;
después, un informe ordenado usable desde terminal sobre ese resumen. Deben existir al menos
dos slices dependientes y ambos hitos deben cerrar sus comprobaciones. Factory decide su
arquitectura y plan mediante el workflow; no precargues fases ni inventes más tareas.

Aceptación conocida: los 10 tests de `test_records.py`, 4 de `test_category_report.py` y los
tests de `test_product_cli.py`, con los ejemplos bajo `examples/`. Sus fuentes y hashes
están en `pilot-contract.json`. La validación final debe ejecutar el producto integrado
desde una copia limpia, repetir la entrada real y conservar la evidencia independiente.
El worker no puede editar estos oráculos para obtener PASS. `USAGE.md` debe documentar
los comandos realmente comprobados antes de fijar el commit final.

No quiero UI, red, dependencias de terceros, instalación de paquetes, persistencia,
otros lenguajes, despliegue, publicación ni cambios automáticos de arquitectura.
Las decisiones de producto necesarias deben presentarse, sin volver a preguntar lo ya
definido aquí. La autorización de ejecución debe ser explícita y sus límites deben abarcar
también discovery, arquitectura y planning; la preparación no autoriza inferencia sin ellos.
