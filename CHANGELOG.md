# Changelog

Notable changes to Shroomap are documented here. Version numbers are development checkpoints, not claims of scientific validation or production readiness.

## [Unreleased] — v0.16 candidate

### Added
- Categorical field-level search-priority assessment combining the existing experimental fruiting-weather index with available habitat evidence.
- Priority explanations distinguishing high-priority candidates, habitat investigation, moderate or conditional candidates, low-weather-priority sites, and excluded or unavailable cases.
- Use of saved grazing confirmation, suitability assessments, observation status and recorded access restrictions in the priority explanation.

### Behaviour and limitations
- Uses provisional index cutoffs of 50 and 75 for workflow categories, **not** empirically calibrated species thresholds.
- Registered pasture remains a habitat proxy; the inventory alone does not verify active grazing or dung availability.
- The priority is explanatory and categorical, not a newly calculated occurrence probability.
- Retains the v0.15 environmental calculation, halo thresholds and local SQLite annotations; no new schema is intended.
- **Status:** Prepared for local testing. Do not create a v0.16 tag until verified.

## [0.15] — 2026-10-29

### Added
- Experimental fruiting-weather profile for *Protostropharia semiglobata* (dung roundhead / gul kragskivling).
- Weather-history diagnostics for rainfall over recent 3-day and 14-day windows, recent cooling, and frost or persistent-freezing events.
- Conditional provisional modifiers to the existing soil-moisture and temperature baseline index.
- Extended score explanation exposing baseline, underlying weather values and adjustment multipliers.

### Changed
- Extended the field and forecast weather index to incorporate these provisional fruiting-weather factors while preserving the field-coordinate weather data path.
- Retained separate habitat eligibility for green halos; the index remains weather-based and does not verify species presence.
- Kept the seasonal score multiplier neutral pending geographically appropriate evidence.

### Limitations
- Temperature and moisture response centres, weights and weather-history multipliers have not been calibrated on verified occurrences.
- Weather-history multipliers may be neutral for many ordinary weather patterns.
- Index values are neither probabilities nor validated predictions.

## [0.14.2] — Initial GitHub checkpoint

### Baseline functionality
- Flask and Leaflet interactive geographic application with an OpenStreetMap basemap.
- Swedish Board of Agriculture meadow and pasture polygons through WFS/WMS, including processing of near-duplicate inventory geometries.
- Field-specific Open-Meteo weather requests, recent history, seven-day forecasts and provider-model coordinate diagnostics.
- Experimental 0–100 weather index based on current shallow soil moisture (50%), seven-day average shallow moisture (20%) and daily minimum/maximum temperature (30% combined).
- Conservative moisture response that avoids treating high volumetric moisture as proof of waterlogging.
- Field-anchored halos with an adjustable index threshold and separate habitat eligibility rules.
- Local SQLite storage for field notes, field visits, custom areas and point observations.

### Known limitations
- Weather and soil moisture are gridded model estimates, not measurements at individual fields.
- Habitat eligibility relies partly on recorded land-use classifications and does not establish actual grazing or dung presence.
- The environmental index was exploratory and not validated against mushroom-fruiting observations.

### Version-history note
- v0.14.2 is the **first repository checkpoint**, not the date on which all baseline features were originally developed.
