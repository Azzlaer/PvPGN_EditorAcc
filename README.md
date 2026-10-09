# LatinBattle · PvPGN Account Manager

Aplicación GUI para Windows, Python 3.11+ y MySQL/MariaDB. Solo se conecta a las tablas `pvpgn_bnet` y `pvpgn_profile`.

## Inicio
1. Instala Python 3.11 o posterior y marca **Add Python to PATH**.
2. Ejecuta `INICIAR.bat`, o usa `pip install -r requirements.txt` y `python main.py`.
3. Indica Host, Puerto, Usuario, Base de datos y Contraseña, y haz clic en **Conectar**.
4. Selecciona la tabla, busca una cuenta, haz doble clic para editar y pulsa Guardar.

La configuración sin contraseña se escribe a `config.json`. Puedes proporcionar la contraseña mediante la variable de entorno `LATINBATTLE_DB_PASSWORD` o teclearla cada vez. No se incluye la contraseña proporcionada en el código ni en los archivos de distribución. Para conexiones remotas se recomienda túnel SSH/VPN o TLS con CA (campo CA TLS). Restringe los privilegios del usuario SQL y protege el servidor MySQL con firewall. Se recomienda rotar cualquier credencial que haya sido compartida.

## Limitaciones y seguridad
- Detecta los campos de las tablas reales mediante `SHOW FULL COLUMNS` e índices por `SHOW INDEX`. El SQL facilitado **no contiene las estructuras de estas tablas**.
- Edición y eliminación requieren clave primaria o índice único con componentes NOT NULL; de lo contrario la tabla se muestra en modo lectura.
- Solo muestra hasta 100 registros por página. Búsqueda `LIKE` sobre hasta 12 columnas textuales no sensibles; para búsquedas especializadas se necesita conocer el esquema.
- Las contraseñas/hashes/tokens están ocultos en la grilla y censurados al exportar CSV. El editor permite introducir valores sensibles, pero **no implementa el algoritmo de hash de PvPGN**: no escribas contraseñas en texto plano sin confirmar cómo las almacena tu versión.
- El CSV no equivale a respaldo MySQL, y no permite restaurar la base. Antes de editar o eliminar, genera un **dump SQL completo** usando Navicat o `mysqldump`/`mariadb-dump`.
- El botón Eliminar borra **solo el registro seleccionado de la tabla actual**, no automáticamente registros relacionados de otras tablas.
- Las modificaciones se envían parametrizadas y con una condición por índice único, con `LIMIT 1`. No se exponen consultas SQL arbitrarias.
- No se implementan altas de cuentas hasta conocer los campos obligatorios, lógica de identificación y algoritmo de hash reales.

## Si falta una tabla
Conéctate al esquema correcto o exporta `SHOW CREATE TABLE pvpgn_bnet` y `SHOW CREATE TABLE pvpgn_profile` para adaptar la aplicación al modelo exacto.
