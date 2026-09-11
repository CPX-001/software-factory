# Software Factory: complemento privado

Este paquete instala Software Factory para tu usuario de Codex. No se publica en
ningún catálogo ni se suben tus proyectos. No necesitas abrir el repositorio de
Software Factory para usarlo.

## Instalar pasando la URL a Codex

La opción preferida es pedirle al chat:

> Instala Software Factory desde https://github.com/CPX-001/software-factory
> siguiendo su README.

El repositorio contiene un marketplace para instalación nativa desde Git, separado
del directorio público de OpenAI. Codex descarga el complemento y el primer arranque
prepara el motor automáticamente. Consulta los [comandos y actualizaciones](codex.md).
Las instrucciones siguientes describen la alternativa mediante ZIP.

## Instalar el ZIP en otro ordenador

Requisitos: **Python 3.11 o posterior**, Git y Codex con tu sesión iniciada.
Usa Linux, macOS o WSL; el motor todavía no admite Windows nativo. La instalación
en macOS debe validarse en ese sistema; la comprobación automatizada se realiza
en Linux. Los proyectos históricos `verified` requieren el aislamiento de Linux.

1. Copia el ZIP privado al ordenador y descomprímelo.
2. En la carpeta extraída `software-factory`, ejecuta:

   ```bash
   python3 install.py
   ```

El instalador descarga las dependencias de Python, incluido el ejecutable Codex
que acompaña al SDK, y comprueba las herramientas MCP sin consumir inferencia.
Necesita conexión a Internet para esa primera preparación. No requiere una
instalación separada de la CLI, `plugin-creator` ni crear `.venv` a mano.

Abre una conversación nueva de Codex **en ese mismo entorno** y escribe:

> Usa Software Factory. Crea mi proyecto en /ruta/a/mis-proyectos/biblioteca.
> Quiero compartir objetos y proponer planes entre amigos…

Puedes borrar el ZIP y la carpeta descomprimida tras instalar. Tampoco se necesita
conservar la copia de desarrollo. La instalación no sincroniza complementos ni
proyectos entre ordenadores; en cada equipo hay que instalar el paquete.
En WSL, la instalación y la sesión de Codex que la utiliza deben ejecutarse en WSL.

## Actualizar y comprobar

Para actualizar, descomprime el nuevo paquete y vuelve a ejecutar `python3 install.py`.
Se conserva el registro de proyectos y se instala el nuevo motor en otra carpeta
si cambió el contenido. Las versiones anteriores se mantienen para que los procesos
ya iniciados puedan terminar. Abre otro chat para cargar las herramientas actualizadas.

Para comprobar una instalación sin hacer llamadas al modelo:

```bash
python3 install.py --check
```

Si necesitas usar una CLI Codex concreta para registrar el complemento:

```bash
python3 install.py --codex /ruta/al/ejecutable/codex
```

No se modifica el proveedor, el modelo ni la facturación de tu cuenta.

## Ubicaciones de la instalación

- Complemento: `~/plugins/software-factory`.
- Catálogo personal: `~/.agents/plugins/marketplace.json`.
- Motor y entorno Python por versión: `~/.local/share/software-factory/runtimes/`.
- Registro de proyectos: `~/.local/state/software-factory`, o `FACTORY_HOME` si ya lo usas.
- Memoria de cada proyecto: `.factory/` dentro de su propia carpeta.

Conserva estas ubicaciones instaladas. El instalador mantiene los otros complementos
y el nombre de tu catálogo personal. No copia credenciales, configuración de Codex,
proyectos, archivos `.env`, entornos virtuales ni datos de desarrollo al ZIP.

## Generar el paquete desde el código

Desde la raíz de la copia de desarrollo:

```bash
python3 scripts/build_codex_plugin.py
```

El ZIP y su checksum SHA-256 se generan en `dist/`. No requiere dependencias de
desarrollo. Para instalar directamente desde esa copia, el mismo instalador está
disponible como `python3 scripts/install_codex_plugin.py` y también crea un motor
independiente del repositorio.
