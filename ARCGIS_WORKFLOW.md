# ArcGIS Pro publishing workflow

1. Run `python publish_outputs.py` to create `Output/pt_study.gpkg` in EPSG:3035.
2. In `Public Transport Study.aprx`, remove the stale file-geodatabase imports and add the six spatial layers from the published GeoPackage.
3. Create maps and layouts named `Urban Area Template` and `Public Transport Template`.
4. In the urban template, symbolize `urban_cells.urban_score` with fixed cross-city class breaks and draw `urban_boundary` above it.
5. In the transport template, symbolize rail lines by `mode`, stations by `departures_per_hr`, and place `isochrones_15min` below the line work. Add a dynamic coverage text element sourced from `city_summary`.
6. Save reusable layer files (`.lyrx`) beside the ArcGIS project.
7. Run `export_arcgis_maps.py` with the ArcGIS Pro Python environment. It duplicates both templates per city, applies each `city_slug` definition query, and exports 12 PDFs at 300 dpi. A layout named `Comparison`, when present, is exported as the thirteenth PDF.
