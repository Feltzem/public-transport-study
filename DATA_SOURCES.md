# Production data source checklist

The repository contains the curated six-city boundaries, the canonical clipped Kontur layer, and cached Microsoft building footprints for all six cities. The remaining production inputs are obtained as follows.

## Automatic sources

- **Microsoft Global ML Building Footprints**: `get_ms_buildings.py` caches `dataset-links.csv` and each requested quadkey tile.
- **OpenStreetMap roads and water**: the urban pipeline prefers prepared local city PBFs and extracts/caches the required features with `osmium`; OSMnx/Overpass is retained as a fallback.
- **OSM PBF files for urban analysis and r5py**: run `python prepare_osm_extracts.py --cities all`. It downloads the appropriate Geofabrik country/region extract and clips it to each curated envelope using `osmium`.
- **OpenStreetMap rail, stations, and walk networks**: queried through OSMnx and cached per city during the transport stage.
- **Oslo GTFS**: Entur's aggregated Norway feed.
- **Copenhagen GTFS**: Rejseplanen's static GTFS feed.
- **Helsinki GTFS**: HSL's static GTFS feed.

## Values the operator must supply

- `TRAFIKLAB_API_KEY`: Stockholm's GTFS Sweden 3 API key.
- `STIB_GTFS_URL` and `DELIJN_GTFS_URL`: direct static-GTFS ZIP URLs **or local ZIP paths** for Brussels.
- `IDFM_GTFS_URL`: direct static-GTFS ZIP URL **or a local ZIP path** for Paris.

Local paths are accepted directly, for example:

```powershell
$env:STIB_GTFS_URL = 'D:\Public Transport Study\Input Data\GTFS\brussels-stib.zip'
$env:DELIJN_GTFS_URL = 'D:\Public Transport Study\Input Data\GTFS\brussels-delijn.zip'
$env:IDFM_GTFS_URL = 'D:\Public Transport Study\Input Data\GTFS\paris-idfm.zip'
$env:TRAFIKLAB_API_KEY = 'your-key'
```

Provider references:

- [Entur stops and timetable data](https://developer.entur.org/stops-and-timetable-data/)
- [Trafiklab GTFS Sweden 3](https://www.trafiklab.se/api/gtfs-datasets/gtfs-sweden/)
- [Digitransit data services](https://digitransit.fi/en/developers/services/6-data-containers/routing-data/)
- [Île-de-France Mobilités GTFS dataset](https://data.iledefrance-mobilites.fr/explore/dataset/offre-horaires-tc-gtfs-idfm/)
- [Geofabrik Europe extracts](https://download.geofabrik.de/europe.html)

## Validation and cartography

- Download **GHS-UCDB R2024A** from the European Commission/JRC for `benchmark_ghsl.py`.
- Use a working ArcGIS Pro installation to create the two template layouts described in `ARCGIS_WORKFLOW.md`, then run `export_arcgis_maps.py` in the Pro Python environment.
