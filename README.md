# XA-UWK · Historial de vuelos

Webapp instalable (PWA) con el historial de vuelos del Pilatus PC-12 **XA-UWK** (Mode S `0D0929`, MSN 1534).

App: https://juanpablogza1.github.io/XA-UWK/

## Qué muestra

- Filtros arriba que controlan toda la app: periodo (7 días, 30 días, 90 días, 12 meses, todo o fechas exactas) y aeropuerto. El periodo activo se ve siempre debajo de los filtros y en el título del mapa.
- Mapa con la **trayectoria real** de cada vuelo. Línea continua = posiciones ADS-B observadas; línea punteada = tramo sin cobertura (estimado); en rojo, estimación de confianza baja. Al tocar una línea o un vuelo se resalta y se ve su detalle.
- Vuelos por mes (tocar un mes lo filtra), aeropuertos y rutas más frecuentes, y la lista de vuelos con hora de despegue y aterrizaje (hora de Monterrey).
- Último contacto del avión (en tierra / en vuelo).

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

## Actualización

`.github/workflows/update.yml` corre cada 3 horas: descarga los días nuevos de adsb.lol (se publican al día siguiente), reconstruye los vuelos y hace commit en `data/` solo si hubo cambios. También se puede correr a mano desde *Actions → Actualizar vuelos XA-UWK → Run workflow*.

Los secrets de OpenSky (`OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET`) ya no se usan y se pueden borrar.

## Licencia de los datos

Los archivos de `data/` derivan de la base de datos de adsb.lol y se distribuyen bajo la [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
