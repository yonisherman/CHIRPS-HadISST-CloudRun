# Changelog

## v0.1.0 — 2026-10-13

- Based on erd-coastwatch/cloud-run-template.
- Two jobs: CHIRPS v2 four-product downloader and HadISST SST/ice downloader.
- Weekly source checks, NetCDF validation, per-object source fingerprints, generation-guarded uploads and persistent ERDDAP reload intent.
- No archive deletions; HadISST decomposition and LISIRD remain outside job scope.
