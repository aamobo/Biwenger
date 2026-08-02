# Tablón de Fichajes — versión alojada

Sincroniza sola con Biwenger cada 3 horas (GitHub Actions) y se ve en
una web pública (GitHub Pages). No hace falta tocar nada después de
configurarlo una vez.

## Archivos
- `index.html` — la web (solo lectura: nadie edita nada aquí).
- `data.json` — movimientos de mercado, los actualiza el robot solo.
- `overrides.json` — nombre de la liga, bolsa inicial, y el valor de
  plantilla inicial de cada manager. **Esto se edita a mano en GitHub**
  (Biwenger no deja verlo por API para otros managers de la liga).
- `sync.py` — el script que hace la sincronización.
- `.github/workflows/sync.yml` — la programación automática.

## Configuración (una sola vez)
1. Sube todos estos archivos a un repositorio público nuevo en GitHub.
2. En **Settings → Secrets and variables → Actions**, añade tres secretos:
   - `BIWENGER_EMAIL`
   - `BIWENGER_PASSWORD`
   - `BIWENGER_LEAGUE_ID`
3. En **Settings → Pages**, activa GitHub Pages sobre la rama `main`, carpeta raíz.
4. En la pestaña **Actions**, entra en "Sincronizar Biwenger" y pulsa
   "Run workflow" una vez para probarlo.
5. Tu web queda en `https://<tu-usuario>.github.io/<nombre-repo>/`.

A partir de aquí, cada 3 horas se sincroniza sola. Para corregir el
valor de plantilla de un manager, edita `overrides.json` directamente
en GitHub (botón del lápiz sobre el archivo) y guarda los cambios.
