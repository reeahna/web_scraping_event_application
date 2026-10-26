# College towns

Towns and event sources the scheduler loads on its own when
`COLLEGE_TOWN_IMPORT_ENABLED=true` (see `app/services/college_towns.py`).

- `cities.csv` lists the towns: `name,slug,state_or_region,universities,timezone`,
  with several universities separated by `;`. A town whose slug already exists is
  left as it is.
- Every other CSV is one day's sources, named `<day>-<city-slug>.csv`, in the
  admin onboarding upload format: `url,name,source_display_name,city_slug,timezone`.

On each scheduler start (so after every deploy) and then daily, missing towns are
created and each file not imported before is queued for onboarding, once. Results
show up under Onboarding in the admin like an uploaded CSV.
