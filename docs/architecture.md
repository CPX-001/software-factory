# Architecture: baseline estructurada y estable

La fase toma el conocimiento confirmado de discovery y diseña una arquitectura general,
no clases, todos los endpoints ni un backlog. Solo se ejecuta mediante `architecture`.
Al completar pasa a `planning`, cuyo handler consume esta baseline sin reevaluarla. Véase [planning](planning.md).

## Comandos

```bash
.venv/bin/python -m factory architecture /ruta/al/proyecto
.venv/bin/python -m factory architecture-status /ruta/al/proyecto
.venv/bin/python -m factory architecture-show /ruta/al/proyecto
.venv/bin/python -m factory architecture-show /ruta/al/proyecto --markdown
.venv/bin/python -m factory architecture-adrs /ruta/al/proyecto
.venv/bin/python -m factory answer /ruta/al/proyecto --id 1 --answer "Opción elegida y condiciones"
.venv/bin/python -m factory architecture /ruta/al/proyecto
```

`architecture` ejecuta o reanuda los pasos pendientes. Las consultas no invocan Codex ni
migran la base. `architecture-status` muestra etapa, llamadas gastadas, routing, gate,
bloqueos, opciones/recomendaciones/consecuencias, decisiones y revisión vigente.
`answer` guarda una respuesta humana con revisión optimista; el siguiente `architecture`
la incorpora. No interpreta una recomendación del modelo como consentimiento.

Si ya existe baseline, ejecutar `architecture` devuelve el estado sin llamadas al SDK ni
al catálogo, sin una nueva revisión. No hay acciones ligadas a la finalización de slices.

## Fuente de verdad y contexto

`factory/architecture_contract.py` define un JSON Schema cerrado y versionado, validado
localmente antes de modificar el estado. Tiene estilo y rationale; componentes con
responsabilidades, ownership y boundaries; dependencias y contratos con protocolos e
invariantes; datos (entidades/relaciones, almacenamiento, consistencia, ciclo de vida y persistencia transitoria/propia/externa);
integraciones y fallos; flujos de información; runtime; seguridad; observabilidad; testing;
restricciones; invariantes; riesgos; supuestos con estrategia de validación; cobertura;
contradicciones; preguntas y decisiones propuestas.

Todos los elementos tienen IDs estables globalmente únicos. `coverage` relaciona claves
de conocimiento autoritativo con elementos concretos y razonamiento. El JSON es la fuente
de verdad. La proyección Markdown completa se genera por código y se guarda junto a la
baseline; se imprime con `architecture-show --markdown` y puede redirigirse a un archivo.

El snapshot fija conceptos activos de discovery: visión, usuarios, capacidades, requisitos,
escala, seguridad, integraciones, restricciones, preferencias, supuestos, criterios de éxito,
exclusiones y decisiones. Conserva su certeza y fundamento. Nunca envía el transcript,
las entradas históricas, eventos ni conversaciones SDK anteriores. Los conceptos
superseded quedan fuera; conflictos o incertidumbre bloqueante impiden comenzar.
Las decisiones humanas recibidas después se añaden como respuestas autoritativas.

Límites: snapshot fuente de 60.000 bytes, salida de 110.000 bytes y contexto de cada llamada
de 180.000 bytes, incluyendo solo propuesta/revisión actuales, respuestas y routing breve.
La historia crece en SQLite, no en contexto. Los excesos fallan sin truncar hechos.
El nivel modular y estos presupuestos están pensados para baselines de sistemas grandes,
no para introducir todo su código o miles de detalles en una única llamada.

## Persistencia, versiones y cambios futuros

Esquema arquitectónico introducido en SQLite v3, conservado en v4 (control de ejecución), con migración aditiva desde v1/v2 en la primera escritura o `init`:

- `architecture_run`: etapa y checkpoints actuales, fuente fijada, propuesta, revisiones,
  presupuesto consumido y motivos de bloqueo.
- `architecture_calls`: rol, contexto exacto, referencias required, respuesta/error y estado
  de cada intento. No se reinyecta como historial al modelo.
- `architecture_decisions`: detalles y vínculo a las decisiones humanas del workflow.
- `architecture_baselines`: arquitectura aceptada, revisión, fingerprint, snapshot fuente,
  fingerprint fuente, revisión crítica, decisiones humanas, gate y proyección Markdown.
- `architecture_current`: puntero inequívoco a la baseline vigente.
- `architecture_adrs`: decisiones significativas aceptadas, baseline a la que pertenecen,
  decisión humana cuando corresponda y vínculo `supersedes_id` para sucesores futuros.
- `architecture_changes`: propuestas futuras, `base_revision`, trigger enumerado,
  evidencia, propuesta, elementos afectados, rationale, decisión humana, estado
  (`proposed`, `accepted`, `rejected`, `superseded`) y revisión resultante al aceptar.

La primera baseline tiene `revision=1`. Su fingerprint es SHA-256 del JSON canónico
(UTF-8, claves ordenadas, separadores fijos, sin NaN). El orden de listas forma parte de
la representación; los timestamps y la revisión del workflow quedan fuera del hash.
El snapshot fuente tiene un hash independiente. Las revisiones futuras podrán enlazar
`parent_revision`; no sobrescribirán baselines ni ADRs históricos.

Una futura slice podrá guardar `(architecture_revision, architecture_fingerprint)`;
`base_revision`, evidencia y `affected_elements` permitirán detectar incompatibilidades
con trabajo pendiente. No se implementa todavía el consumidor de esas referencias.

Los únicos triggers preparados para cambios son `new_boundary`, `persistence_change`,
`structural_runtime`, `public_protocol`, `incompatible_requirement`, `invalidated_decision`,
`repeated_architectural_failure` y `architectural_gate`. No existe `slice_completed`.
No hay todavía API ni loop que cree o acepte esos cambios: la tabla prepara su persistencia.

## Decisiones y ADRs

Las decisiones propuestas registran elección, alternativas, evidencia, rationale y
consecuencias. Generan ADR las clasificadas como estructura/estilo, persistencia,
comunicación, protocolo, aislamiento, autenticación, boundary o runtime. Se exige al menos
un ADR de estilo y otro de almacenamiento cuando existen datos persistentes.
Las decisiones `minor` quedan en el JSON con rationale y no generan ADR.

El arquitecto identifica semánticamente los tradeoffs que necesitan al usuario y produce
`unresolved_questions`, cada una con clave estable, al menos dos opciones, recomendación y
consecuencias. Se persisten antes de detenerse. Las respuestas se incorporan en un nuevo
turno, sin reescribir la pregunta, y las decisiones correspondientes deben enlazar
`human_key`. Código verifica el vínculo y la existencia de respuesta; el juicio de si la
elección refleja correctamente la respuesta sigue siendo semántico, revisable en la baseline.

## Revisión acotada

Roles ejecutados en hilos SDK nuevos e independientes:

1. `propose`: propuesta inicial, pausando por decisiones humanas si es necesario.
2. `critic`: solo riesgos importantes, contradicciones, requisitos descubiertos sin cubrir
   y complejidad innecesaria. Sus hallazgos deben referenciar elementos/conceptos existentes.
3. `reconcile`: una reconciliación si hay hallazgos, o si el gate detecta carencias.
4. `critic_final`: comprobación final de la propuesta reconciliada cuando corresponde revisar.

Máximo dos pases del crítico y una reconciliación lógica. Una pausa humana puede necesitar
otro turno del mismo rol, pero existe además un máximo global persistente de ocho intentos,
incluidos fallidos/interrumpidos. No se reinicia al reanudar. Un error de SDK o contrato
interrumpe el comando sin reintentos automáticos del controlador.

Después del límite, los hallazgos importantes se convierten en decisiones visibles.
La respuesta literal `accept` acepta riesgos/complejidad residuales y queda archivada como
evidencia humana. `reject` bloquea la baseline. Ni `accept` puede dispensar contradicciones
estructurales conocidas o requisitos sin cubrir: quedan bloqueados. No se abre otro ciclo
para resolverlos ni se proporciona un reset que eluda el presupuesto. Requieren un esfuerzo
nuevo de diseño explícitamente delimitado, fuera del controlador inicial actual.

Una clasificación determinista conservadora evita el crítico solo en utilidades pequeñas,
locales/offline, de un usuario, sin integraciones declaradas ni señales importantes de
riesgo/complejidad. La incertidumbre recibe revisión. La forma de la propuesta también
puede exigirla; una revisión ya exigida no se elimina por simplificar después. Es una
heurística explicable (se guardan motivos), no un clasificador semántico infalible.

## Quality gate

Código comprueba completitud/coherencia mínima, sin aceptar un simple `done`:

- cobertura de cada concepto activo mediante referencias válidas, con cobertura específica
  por elementos de seguridad para requisitos de esa categoría;
- ninguna decisión humana pendiente ni pregunta de arquitectura sin resolver;
- IDs únicos, responsabilidades, ownership y boundaries no vacíos;
- dependencias con extremos y contratos reales, participantes coherentes y flujos
  respaldados por dependencias (también permiten el retorno de información); ownership de datos y contratos de integraciones válidos;
- runtime y pruebas cubren todos los componentes; existen seguridad, observabilidad,
  invariantes y restricciones;
- no hay contradicciones estructurales conocidas; el crítico no deja carencias de
  cobertura/consistencia pendientes;
- riesgos críticos mitigados o aceptados explícitamente por un humano; supuestos fuente
  registrados con fundamento y validación;
- ADRs fundamentales, evidencia válida, incorporación trazable de respuestas humanas;
- revisión correspondiente superada o aceptación humana admisible del riesgo residual.

Si necesita corrección, reutiliza como máximo la única reconciliación disponible. Si ya
se gastó, queda bloqueado. La calidad semántica del texto y el descubrimiento de riesgos
siguen correspondiendo al arquitecto y al crítico; el gate no demuestra corrección formal.

Baseline, ADRs, proyección, cierre de architecture y transición a planning se confirman
atómicamente con su evento. Un fallo no deja una arquitectura parcialmente aceptada.

## SDK, skills y recuperación

`CodexArchitecture` conserva `gpt-5.6-terra`, el SDK instalado y el login ChatGPT existente.
Usa directorio temporal, sandbox de lectura, aprobaciones denegadas e hilos efímeros nuevos.
No modifica el proyecto ni cambia a un cliente API. Required se envía mediante el tipo
nativo `SkillInput(name, path)` del SDK; seleccionar un nombre o afirmar que se ha usado
no sustituye su activación. Las skills conservan sus propias instrucciones y dependencias.

El controlador consulta `SkillRouter` para el rol y riesgo correspondientes, usando catálogo
real `skills/list`, raíces y overrides del proyecto. Falla antes de llamar al modelo si el
catálogo falla o falta una required. Envía solamente routing compacto y referencias required.
Recommended son candidatas opcionales; Codex puede descubrir otras cuando aporten valor.
No selecciona `archify` por estar en architecture: la política de diagramas requiere una
necesidad explícita de diagrama. No carga un catálogo completo ni obliga a un panel.

Un lock de proceso impide llamadas simultáneas de architecture; se libera al morir el
proceso. Cada intento se registra antes de invocar Codex. La revisión optimista impide
aplicar una respuesta obsoleta si se responde una decisión concurrentemente. Reanudar usa
los checkpoints; no repite pasos confirmados. Una muerte después del resultado pero antes
del commit puede repetir esa llamada, siempre contabilizada dentro del presupuesto.

## Verificación

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Los tests mockean el modelo y el SDK: no consumen cuota. Cubren baseline, persistencia,
routing/overrides/required nativas, decisiones y respuestas, esquema inválido,
interrupción, concurrencia, rollback, gates, revisión/límites, proyectos triviales,
revisión/fingerprint, ADRs, migración y CLI entre procesos. Una prueba real opcional debe
realizarse separadamente y con un proyecto temporal; nunca forma parte de la suite.
