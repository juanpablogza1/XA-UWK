# XA-UWK · Historial de vuelos

Webapp instalable (PWA, solo vertical) que muestra el historial de vuelos del Pilatus PC-12 **XA-UWK** (Mode S / ICAO24 `0D0929`, MSN 1534).

- **Datos:** OpenSky Network (ADS-B). Un workflow de GitHub Actions consulta la API cada 6 h y guarda los vuelos en `data/flights.json`; el historial se acumula en el repo.
- **Aeropuertos:** nombres y coordenadas desde OurAirports.
- **App:** KPIs, mapa de rutas, vuelos por mes, rutas frecuentes, historial filtrable y enlace a la trayectoria de cada día en ADS-B Exchange.

## Configuración (una sola vez)

1. **Repo:** crea un repo en GitHub (p. ej. `xauwk-vuelos`) y sube todo el contenido de esta carpeta, incluida `.github/`.
2. **Credenciales OpenSky:** crea una cuenta gratis en opensky-network.org → *Account* → *API Client* → crea un cliente y copia `client_id` y `client_secret`.
3. **Secrets:** en el repo, *Settings → Secrets and variables → Actions → New repository secret*:
   - `OPENSKY_CLIENT_ID`
   - `OPENSKY_CLIENT_SECRET`
4. **Permisos del workflow:** *Settings → Actions → General → Workflow permissions →* "Read and write permissions".
5. **GitHub Pages:** *Settings → Pages →* Source "Deploy from a branch", rama `main`, carpeta `/ (root)`.
6. **Primera carga:** *Actions → Actualizar vuelos XA-UWK → Run workflow*, con `backfill_days = 30`.
7. **Instalar:** abre la URL de Pages en el celular → "Agregar a pantalla de inicio".

## Limitaciones

- OpenSky calcula los vuelos en un proceso nocturno: un vuelo de hoy aparece normalmente al día siguiente.
- La cobertura depende de los receptores voluntarios; en zonas sin receptores el vuelo puede no aparecer o salir sin origen/destino.
- Origen y destino son estimaciones de OpenSky.
- La API REST no da historial de años: el historial crece desde que activas el workflow. Para fechas anteriores usa los enlaces de ADS-B Exchange / FlightAware de la app.
- Créditos diarios de OpenSky: la consulta cada 6 h con 4 días de ventana gasta muy poco.
