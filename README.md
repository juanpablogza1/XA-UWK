# XA-UWK · Historial de vuelos

Webapp instalable (PWA) con el historial de vuelos del Pilatus PC-12 **XA-UWK** (Mode S `0D0929`, MSN 1534).

App: https://juanpablogza1.github.io/XA-UWK/

## Qué muestra

La app tiene tres pestañas (abajo en el celular, arriba en la computadora). Los filtros de periodo (7 días, 30 días, 90 días, 1 año, todo o fechas exactas) y de aeropuerto son los mismos en las tres, y el periodo activo siempre se ve.

- **Mapa:** la trayectoria real de cada vuelo del periodo. Línea continua = observada; punteada = tramo sin señal (estimado); roja = estimación de confianza baja. Tocar una ruta abre su ficha (horarios, distancia, altitud, cómo se determinó el origen/destino) con flechas para pasar al vuelo más reciente o más antiguo; tocar un aeropuerto permite filtrar por él.
- **Vuelos:** lista por mes con buscador (código, ciudad o nombre de aeropuerto) y la sección **Hoy** con los vuelos seguidos en vivo.
- **Resumen:** totales, calendario de días con vuelo de los últimos 12 meses (tocar un día o un mes lo filtra), vuelos y horas por mes, destinos y rutas frecuentes, y los periodos en que el avión no transmitió.
- **Banner "En vuelo ahora":** aparece arriba solo mientras el avión está volando; al tocarlo, el mapa sigue al avión y muestra altitud, velocidad y rumbo en vivo.

## De dónde salen los datos

- **Trayectorias:** historial abierto de [adsb.lol](https://adsb.lol), publicado a diario en GitHub (`adsblol/globe_history_AAAA`), licencia **ODbL 1.0**. Cada día es un archivo de ~3.5 GB; `scripts/adsb_history.py` ubica con peticiones HTTP Range el pedazo donde está este avión y lee solo unas decenas de MB. Las trazas del avión se guardan en `data/raw/`.
- **Aeropuertos:** [OurAirports](https://ourairports.com/data/) (dominio público).
- **Mapa:** © OpenStreetMap.

## Cómo se identifica el origen y destino

| Método | Significado |
|---|---|
| `tierra` | El avión se vio en tierra (o a menos de 700 ft sobre la pista) en ese aeropuerto. Dato observado. |
| `escala` | Se perdió la señal al bajar y reapareció subiendo en la misma zona: el aeropuerto se estima con ambas trayectorias (rumbo, altitud, distancia y tiempo disponible). |
| `secuencia` | Coincide con el destino del vuelo anterior o el origen del siguiente, y la trayectoria es compatible. |
| `trayectoria` | Estimado solo con el rumbo y el ascenso/descenso al perder la señal. |

Cada estimación trae su confianza (alta, media o baja) y, si es baja, las alternativas más probables. En zonas sin receptores ADS-B, como el centro de México, el destino real no se puede confirmar; la app lo marca como incierto (`¿?`).

## Actualización y seguimiento en vivo

- `.github/workflows/update.yml` corre cada 15 min: descarga los días nuevos de adsb.lol (se publican al día siguiente), reconstruye los vuelos, revisa la posición actual y hace commit en `data/` solo si hubo cambios. Si el avión está volando, inicia el seguimiento en vivo.
- `.github/workflows/live.yml` sigue al avión mientras vuela: consulta su posición cada 10 s (api.adsb.lol, respaldo adsb.fi) y la publica en la rama `live` (un solo commit que se reemplaza). Termina 75 min después de aterrizar si no vuelve a despegar.
- En la app: el botón ⟳ lanza la actualización y recarga los datos al terminar; el banner **En vuelo ahora** aparece solo mientras el avión vuela.

Para usar los botones desde la app hace falta un *fine-grained token* de GitHub (Ajustes ⚙︎ en la app): solo el repositorio XA-UWK, permisos **Actions: Read and write** y **Contents: Read-only**. Se guarda únicamente en el dispositivo. Sin token, la app igual muestra el vivo (con unos minutos de retraso) y los datos se actualizan solos.

Los secrets de OpenSky (`OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET`) ya no se usan y se pueden borrar.

## Licencia de los datos

Los archivos de `data/` derivan de la base de datos de adsb.lol y se distribuyen bajo la [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
