# Changelog

## 0.3.0 — Photogrammetric connection (2026-10-11)

### Added
- `stockopoly.metrology`: local standards (personal; applied only with `photographed_owner` equal to the owner), published references and the EXIF focus prior ground photo scales through the existing WLS solver; every source is logged in `metrology_observations` under the photo's `sha256:` key. `metrology_obs_public` (no owner/basis, no personal pixel spans) mirrors to the shared Supabase `metrology_observations`. `export_error_stats` writes the unit-free statistics Perceptopoly reads.
- CLI `python -m stockopoly metrology import-standards | apply-standard | exif | report | export-stats`; API `/api/metrology/*` (403 for someone else's standard).
- EXIF reader takes SubjectDistance (`focus_distance_m`). Setting `metrology_exif_prior` (default off) feeds that prior to the solver.

### Fixed
- WLS bridge posted warehouse metrology to QUIPU as `perceptopoly` (game region); it now posts as `stockopoly` (operations) and no longer sends placeholder `standoff_m`/`range_m` of 1.0.
