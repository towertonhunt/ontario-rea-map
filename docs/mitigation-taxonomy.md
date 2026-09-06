# Mitigation Taxonomy — Classification Framework

Schema for the early-stage EA mitigation prediction tool. Automated
condition-extraction maps raw approval-condition text (IAAC decision statements,
BC EAO certificate condition tables, Ontario REA/ECA/EA + mine closure plans,
Quebec REE decrees, Atlantic approvals) into the structured records defined here.
The matching engine (see `docs/mitigation-tool-plan.md`) then joins extracted
conditions to a proposed project by **archetype** × **discipline** × **spatial
trigger**.

This is a controlled vocabulary. Extraction code must map to these exact enum
values (snake_case, listed in each section) and must not invent new ones without
adding them here first. When a raw condition does not fit, tag it
`discipline: other` / `measure_type: other` and preserve verbatim text — do not
force-fit.

---

## 1. Project archetypes

Aligned to the map's `category` field in `data/projects_canada.geojson`, then
subdivided where the regulatory treatment materially differs. `archetype` is the
leaf value; `category` is retained for map linkage.

| category | archetype (enum) | notes |
|---|---|---|
| wind | `wind_onshore` | turbines, met towers, collector lines |
| wind | `wind_offshore` | marine/lake-bed; distinct fish/marine mammal regime |
| solar | `solar_pv_ground` | ground-mount arrays, land-cover conversion |
| solar | `solar_rooftop` | minimal EA footprint; often screened out |
| biogas | `biogas_anaerobic_digestion` | feedstock, odour, digestate handling |
| hydro | `hydro_large_reservoir` | dam + impoundment, flow regime change |
| hydro | `hydro_run_of_river` | diversion, limited storage |
| hydro | `hydro_pumped_storage` | dual reservoir, entrainment |
| mining | `mine_open_pit_metal` | pit, waste rock, tailings |
| mining | `mine_underground` | shaft/decline, subsidence, dewatering |
| mining | `mine_quarry_aggregate` | pits & quarries, blasting, dust |
| mining | `mine_placer_insitu` | placer / in-situ leach |
| oil_gas | `oilgas_pipeline` | linear ROW, watercourse crossings |
| oil_gas | `oilgas_wellpad_facility` | drilling, flaring, produced water |
| oil_gas | `oilgas_lng_terminal` | marine + air + storage |
| nuclear | `nuclear_generation` | reactor, thermal plume, radiological |
| nuclear | `nuclear_waste` | DGR / storage, long-term stewardship |
| transmission | `energy_transmission` | HV lines & stations (linear ROW) — own map category since 2026-09-06 |
| energy_other | `energy_thermal_gas` | gas/thermal generating stations |
| energy_other | `energy_storage_battery` | BESS, fire/hazmat focus |
| transport | `transport_highway` | roads, interchanges |
| transport | `transport_rail` | rail lines & yards |
| transport | `transport_port_marine` | ports, wharves, dredging |
| transport | `transport_transit` | LRT/subway/BRT |
| transport | `transport_airport` | runways, noise contours |
| water | `water_treatment_wwtp` | water/wastewater treatment |
| water | `water_dam_flood` | flood control, dams (non-power) |
| water | `water_diversion` | intakes, diversions, desalination |
| industrial | `industrial_manufacturing` | plants, foundries, mills |
| industrial | `industrial_chemical` | chemical/petrochemical |
| waste | `waste_landfill` | landfills, leachate |
| waste | `waste_incineration_efw` | energy-from-waste, emissions |
| waste | `waste_transfer_recycling` | transfer/MRF |
| agriculture | `agriculture_intensive_livestock` | barns, manure, nutrient mgmt |
| agriculture | `agriculture_crop_irrigation` | land conversion, water taking |
| tourism | `tourism_resort_recreation` | resorts, marinas, ski |
| other | `other` | unclassified; keep verbatim category |

Extraction note: many source docs describe multi-component projects (e.g. a mine
that includes a transmission line and an access road). Emit **one record per
distinct measure**, tagging `project_archetype` with the primary archetype of the
approval; use the `trigger` field to note the specific component when relevant.

---

## 2. Discipline domains

The environmental/social receptor a measure protects. Enum values:

| discipline (enum) | scope |
|---|---|
| `surface_water` | rivers, lakes, drainage, water quality/quantity, sediment |
| `groundwater` | aquifers, wells, dewatering, drawdown, seepage |
| `fish_fish_habitat` | fish, fish habitat, HADD, Fisheries Act s.34/35 |
| `wetlands` | evaluated/unevaluated wetlands, bogs, fens, marshes |
| `vegetation_ecosystems` | flora, ELC communities, forests, invasive species |
| `wildlife_birds` | mammals, herptiles, migratory birds (MBCA), bats |
| `species_at_risk` | SARA / provincial ESA listed species & critical habitat |
| `air_quality` | dust, PM, NOx/SOx, odour, emissions |
| `noise_vibration` | construction/operational noise, blasting vibration |
| `light` | light pollution / spill, aviation lighting, wildlife attraction |
| `soils_terrain` | soils, geotech, erosion, permafrost, contaminated sites |
| `waste_hazmat` | solid/liquid waste, tailings, spills storage, hazmat |
| `accidents_malfunctions` | emergency response, spill contingency, failure modes |
| `human_health` | drinking water, country foods, health risk assessment |
| `socio_economic` | employment, housing, services, navigation, land use |
| `indigenous_rights_tluse` | rights, traditional land/resource use, cultural sites |
| `archaeology_heritage` | archaeological & built-heritage resources |
| `visual_landscape` | viewsheds, aesthetics, dark-sky |
| `climate_ghg` | GHG emissions, carbon, climate resilience/adaptation |
| `cumulative_effects` | regional/cumulative effects assessment & mgmt |
| `closure_postclosure` | decommissioning, reclamation, long-term stewardship |
| `other` | anything unmapped; preserve verbatim |

Notes:
- `indigenous_rights_tluse` is kept distinct from `socio_economic` and
  `archaeology_heritage` deliberately — condition frequency and legal weight
  differ and it drives the engagement output.
- A single condition may serve multiple disciplines (e.g. a riparian buffer
  serves `surface_water` + `fish_fish_habitat` + `wetlands`). Emit the **primary**
  discipline in `discipline` and list secondaries in an optional
  `discipline_secondary` array (see schema §4).

---

## 3. Measure types

The regulatory instrument class of the measure — the mitigation hierarchy plus
the administrative wrappers regulators actually use. Enum values:

| measure_type (enum) | definition | typical verbs in source text |
|---|---|---|
| `avoidance` | eliminate effect by siting or timing | "shall not site", "avoid", "no work between…" |
| `minimization` | reduce effect by design/technology | "design shall", "install", "limit to" |
| `mitigation` | operational controls during activity | "implement", "maintain", "control" |
| `compensation_offset` | offset residual effect | "offset", "habitat compensation", "replacement" |
| `management_plan` | prepare/implement a named plan | "develop and implement a … Plan" |
| `monitoring_followup` | measure effects & verify predictions | "monitor", "follow-up program", "report annually" |
| `financial_assurance` | bond/security for obligations | "financial assurance", "security", "reclamation bond" |
| `engagement` | consult/notify/accommodate parties | "consult", "notify", "provide to Indigenous groups" |
| `other` | administrative/unclassified | — |

The first four map to the classic mitigation hierarchy
(**avoid → minimize → mitigate → compensate**). Extraction should prefer the most
specific type; a condition that both requires a plan and prescribes a control is
`management_plan` if the plan is the enforceable object, otherwise `mitigation`.

### Named management plan types (`plan_required` vocabulary)

When `measure_type = management_plan` (or a measure references a plan), set
`plan_required` to one of these normalized names (extend list as new plan names
recur ≥3×). This is the join key for "what plans will I need to write".

- `erosion_sediment_control_plan` (ESC / ESCP)
- `water_management_plan`
- `surface_water_quality_monitoring_plan`
- `groundwater_monitoring_plan`
- `fish_habitat_offsetting_plan` (Fisheries Act authorization)
- `fisheries_act_monitoring_plan`
- `wetland_compensation_plan`
- `vegetation_invasive_species_management_plan`
- `wildlife_management_plan`
- `bird_bat_monitoring_plan` (post-construction mortality monitoring)
- `species_at_risk_mitigation_plan` (SARA/ESA)
- `air_quality_dust_management_plan`
- `noise_management_plan`
- `blasting_management_plan`
- `spill_prevention_contingency_plan`
- `emergency_response_plan`
- `waste_management_plan`
- `tailings_management_plan`
- `mine_closure_reclamation_plan`
- `contaminated_sites_soil_management_plan`
- `human_health_risk_management_plan`
- `traffic_management_plan`
- `construction_environmental_management_plan` (CEMP — parent/umbrella)
- `indigenous_engagement_plan`
- `heritage_archaeology_management_plan`
- `cumulative_effects_management_plan`
- `follow_up_monitoring_program` (federal IAA s.91 generic)
- `null` (no plan required)

---

## 4. Condition record schema

One JSON object per extracted condition-measure. Fields:

```json
{
  "condition_id": "string  // stable id: <jurisdiction>-<source_doc>-<seq>",
  "source_doc": "string  // filename or URL of approval/certificate",
  "jurisdiction": "enum: federal | bc | on | qc | ns | nb | nl | pe | yt | nt | nu",
  "project_id": "string  // FK to projects_canada.geojson feature id, or null",
  "project_archetype": "enum: see §1",
  "discipline": "enum: see §2  // primary",
  "discipline_secondary": ["enum: see §2  // optional, may be empty"],
  "measure_type": "enum: see §3",
  "trigger": {
    "spatial": "string|null  // e.g. 'within 30 m of a watercourse'",
    "temporal": "string|null // e.g. 'April 1 – July 31 breeding window'",
    "receptor": "string|null // e.g. 'nearest noise receptor', 'SAR: Blanding's Turtle'"
  },
  "measure_text": "string  // verbatim condition text, cleaned of headers",
  "plan_required": "enum: see §3 plan vocabulary | null",
  "timing": "enum: pre_construction | construction | operation | closure | post_closure | all_phases",
  "verification": {
    "who": "string|null  // e.g. 'IAAC', 'EAO', 'MECP District', 'DFO', 'Qualified Person'",
    "how": "string|null  // e.g. 'annual report', 'notification 60 days prior', 'independent audit'"
  }
}
```

Extraction rules:
- `condition_id` must be deterministic and idempotent so re-runs don't duplicate.
- `measure_text` is verbatim (source of truth); all enums are the classification
  layer over it. Never paraphrase into `measure_text`.
- If a single numbered condition contains several measures, split into multiple
  records sharing a `source_doc` and a common id prefix.
- `trigger.*` are free text at v1 (regex/LLM-extracted), but normalize distances to
  metres and dates to `MM-DD` where possible so the spatial engine can compare.
- `jurisdiction` uses provincial postal-style abbreviations; `federal` for IAAC.
- Unknown/empty fields are `null`, never `""` or omitted.

---

## 5. Worked matrix — near-universal condition families per archetype

These are the **priors**: the 8–12 condition families the tool asserts by default
for each archetype before real extracted conditions refine frequencies. Each cell
is a (discipline, measure_type, typical plan) prior. Generic but concrete.

### wind_onshore
1. `wildlife_birds` / monitoring — post-construction bird & bat mortality monitoring (`bird_bat_monitoring_plan`)
2. `wildlife_birds` / avoidance — vegetation clearing outside migratory bird nesting window (MBCA)
3. `wetlands` / avoidance — turbine & road setbacks from evaluated wetlands
4. `noise_vibration` / minimization — operational noise limits at nearest receptors (e.g. 40 dBA)
5. `species_at_risk` / mitigation — SAR (bat/raptor/grassland bird) mitigation (`species_at_risk_mitigation_plan`)
6. `surface_water` / management_plan — `erosion_sediment_control_plan` for access roads/collector lines
7. `soils_terrain` / mitigation — decommissioning & site restoration commitments
8. `socio_economic` / minimization — shadow flicker limits at residences
9. `light` / minimization — aviation lighting minimized (radar-activated where allowed)
10. `closure_postclosure` / financial_assurance — decommissioning security

### solar_pv_ground
1. `surface_water` / management_plan — `erosion_sediment_control_plan`
2. `wetlands` / avoidance — setbacks from wetlands/watercourses
3. `vegetation_ecosystems` / minimization — native ground-cover / pollinator seeding, invasive control
4. `wildlife_birds` / avoidance — clearing outside nesting window
5. `species_at_risk` / mitigation — SAR survey + mitigation
6. `visual_landscape` / minimization — glare/visual screening
7. `soils_terrain` / mitigation — minimize grading, preserve topsoil
8. `agriculture`(socio_economic) / minimization — prime agricultural land / drainage protection
9. `closure_postclosure` / management_plan — decommissioning & restoration

### mine_open_pit_metal
1. `surface_water` / management_plan — `water_management_plan` (site-wide water balance)
2. `waste_hazmat` / management_plan — `tailings_management_plan` (dam safety, OMS)
3. `fish_fish_habitat` / compensation_offset — `fish_habitat_offsetting_plan` (Fisheries Act auth)
4. `groundwater` / monitoring — `groundwater_monitoring_plan` (dewatering drawdown)
5. `air_quality` / mitigation — `air_quality_dust_management_plan` (haul roads, crushing)
6. `noise_vibration` / management_plan — `blasting_management_plan` (vibration/airblast limits)
7. `human_health` / assessment — country-foods / metals HHRA & monitoring
8. `closure_postclosure` / financial_assurance — `mine_closure_reclamation_plan` + closure security
9. `accidents_malfunctions` / management_plan — `spill_prevention_contingency_plan` + tailings failure ERP
10. `indigenous_rights_tluse` / engagement — TLU study, ongoing engagement, benefits
11. `species_at_risk` / mitigation — SAR (caribou often) mitigation plan
12. `cumulative_effects` / monitoring — regional cumulative effects follow-up

### mine_quarry_aggregate
1. `air_quality` / mitigation — `air_quality_dust_management_plan`
2. `noise_vibration` / management_plan — `blasting_management_plan` (limits at receptors)
3. `groundwater` / monitoring — water table monitoring (below-water extraction)
4. `surface_water` / management_plan — `erosion_sediment_control_plan`, settling ponds
5. `soils_terrain` / management_plan — progressive rehabilitation / `mine_closure_reclamation_plan`
6. `socio_economic` / management_plan — `traffic_management_plan` (haul routes)
7. `species_at_risk` / mitigation — SAR setbacks/timing
8. `closure_postclosure` / financial_assurance — rehabilitation security

### oilgas_pipeline
1. `fish_fish_habitat` / minimization — trenchless/HDD watercourse crossings; timing windows
2. `surface_water` / management_plan — `erosion_sediment_control_plan`, crossing plans
3. `wetlands` / mitigation — wetland crossing & reclamation methods
4. `wildlife_birds` / avoidance — clearing outside nesting window; den/nest sweeps
5. `species_at_risk` / mitigation — `species_at_risk_mitigation_plan`
6. `soils_terrain` / management_plan — soil handling/reclamation; contaminated-soils mgmt
7. `accidents_malfunctions` / management_plan — `emergency_response_plan`, `spill_prevention_contingency_plan`
8. `vegetation_ecosystems` / mitigation — invasive species / ROW revegetation
9. `archaeology_heritage` / avoidance — chance-find protocol, pre-clearing survey
10. `indigenous_rights_tluse` / engagement — construction monitors, TLU

### energy_transmission
1. `wildlife_birds` / minimization — avian collision markers / raptor-safe design
2. `wetlands` / avoidance — tower siting setbacks; matting for access
3. `vegetation_ecosystems` / mitigation — ROW clearing/veg management, invasives
4. `fish_fish_habitat` / minimization — watercourse crossing methods & timing
5. `surface_water` / management_plan — `erosion_sediment_control_plan`
6. `species_at_risk` / mitigation — SAR mitigation
7. `visual_landscape` / minimization — routing/viewshed
8. `archaeology_heritage` / avoidance — survey + chance-find protocol

### transport_highway
1. `surface_water` / management_plan — `erosion_sediment_control_plan`, stormwater management
2. `fish_fish_habitat` / minimization — culvert/bridge design, in-water timing windows
3. `wildlife_birds` / mitigation — wildlife crossings/fencing; clearing timing window
4. `noise_vibration` / minimization — noise barriers at receptors
5. `air_quality` / mitigation — construction dust control
6. `species_at_risk` / mitigation — SAR exclusion fencing / relocation
7. `archaeology_heritage` / avoidance — Stage 1–2 assessment + chance find
8. `wetlands` / compensation_offset — `wetland_compensation_plan`
9. `socio_economic` / management_plan — `traffic_management_plan`

### transport_port_marine
1. `fish_fish_habitat` / compensation_offset — dredging effects, HADD offsetting
2. `surface_water` / minimization — sediment/turbidity controls, silt curtains
3. `waste_hazmat` / management_plan — dredged-material / contaminated sediment management
4. `wildlife_birds` / avoidance — marine mammal/bird timing & exclusion
5. `air_quality` / mitigation — vessel & equipment emissions
6. `noise_vibration` / minimization — underwater noise (pile driving) limits
7. `accidents_malfunctions` / management_plan — `spill_prevention_contingency_plan`
8. `socio_economic` / minimization — navigation & commercial fishery access

### hydro_run_of_river / hydro_large_reservoir
1. `fish_fish_habitat` / minimization — fish passage, entrainment screening, ramping rates
2. `surface_water` / mitigation — minimum instream/environmental flows
3. `fish_fish_habitat` / compensation_offset — `fish_habitat_offsetting_plan`
4. `human_health` / monitoring — reservoir methylmercury monitoring (large reservoir)
5. `wetlands` / compensation_offset — inundation offsets
6. `species_at_risk` / mitigation — SAR mitigation
7. `indigenous_rights_tluse` / engagement — TLU, navigation, fishery access
8. `closure_postclosure` / management_plan — dam safety / decommissioning

### waste_landfill
1. `groundwater` / monitoring — leachate & `groundwater_monitoring_plan` (contaminant attenuation zone)
2. `surface_water` / management_plan — leachate collection, stormwater, ESC
3. `air_quality` / mitigation — landfill gas collection; odour management
4. `human_health` / assessment — HHRA; buffer to receptors
5. `wildlife_birds` / mitigation — bird/vector control
6. `noise_vibration` / minimization — operating-hour & equipment noise limits
7. `socio_economic` / management_plan — `traffic_management_plan`
8. `closure_postclosure` / financial_assurance — closure & post-closure care security

### waste_incineration_efw / industrial_chemical / industrial_manufacturing
1. `air_quality` / minimization — stack emission limits + continuous monitoring (CEMS)
2. `human_health` / assessment — HHRA (dioxins/metals for EFW)
3. `waste_hazmat` / management_plan — `waste_management_plan`, ash/residue handling
4. `surface_water` / management_plan — process water/stormwater, ESC
5. `noise_vibration` / minimization — receptor noise limits
6. `accidents_malfunctions` / management_plan — `spill_prevention_contingency_plan`, ERP
7. `climate_ghg` / monitoring — GHG quantification & reporting
8. `soils_terrain` / management_plan — contaminated-sites/soil management

### nuclear_generation / nuclear_waste
1. `human_health` / monitoring — radiological environmental monitoring program
2. `surface_water` / minimization — thermal plume / cooling-water effects, impingement/entrainment
3. `fish_fish_habitat` / compensation_offset — intake effects offsetting
4. `accidents_malfunctions` / management_plan — ERP, severe-event contingency
5. `waste_hazmat` / management_plan — radioactive waste management
6. `closure_postclosure` / financial_assurance — decommissioning fund / long-term stewardship
7. `indigenous_rights_tluse` / engagement — long-horizon engagement (esp. `nuclear_waste`)
8. `cumulative_effects` / monitoring — regional follow-up

### agriculture_intensive_livestock / biogas_anaerobic_digestion
1. `surface_water` / management_plan — nutrient/manure management, ESC
2. `groundwater` / monitoring — well/aquifer monitoring
3. `air_quality` / mitigation — odour management; digestate handling
4. `human_health` / minimization — separation distances (MDS)
5. `waste_hazmat` / management_plan — `waste_management_plan` (feedstock/digestate)
6. `wildlife_birds` / avoidance — clearing timing window

Matrix usage: on a query, seed the register with the archetype's prior rows, then
overlay (a) real extracted conditions for that archetype with observed frequency,
and (b) spatial-trigger-fired disciplines from §6. Priors with no extracted
support are shown as "expected (no precedent match yet)".

---

## 6. Spatial trigger → discipline mapping

The baseline engine runs point-in-polygon / buffer queries against site geometry
(see the layer list in `docs/mitigation-tool-plan.md` §2) and fires disciplines.
Each firing raises the discipline's priority in the register and attaches the
trigger to `trigger.spatial`. Buffers are defaults; refine from extracted setbacks.

| spatial condition (layer / query) | fires disciplines | typical measure family |
|---|---|---|
| site intersects **evaluated wetland** (or ≤120 m) | `wetlands`, `surface_water` | setback / `wetland_compensation_plan` |
| site intersects **unevaluated wetland** | `wetlands` | wetland evaluation trigger + setback |
| crosses / ≤100 m of **watercourse** | `fish_fish_habitat`, `surface_water` | crossing method, timing window, `erosion_sediment_control_plan` |
| ≤30 m of **any waterbody shoreline** | `surface_water`, `fish_fish_habitat` | riparian buffer |
| within **species-at-risk range / critical habitat** | `species_at_risk`, `wildlife_birds` | SAR survey + `species_at_risk_mitigation_plan` (ESA/SARA permit) |
| within **ANSI / conservation reserve / provincial park** buffer | `vegetation_ecosystems`, `wildlife_birds` | avoidance / restricted activity |
| within **significant wildlife habitat / deer yard / stick nest** | `wildlife_birds` | seasonal timing, setbacks |
| forested land-cover (ELC/FRI) present | `vegetation_ecosystems`, `wildlife_birds` | clearing outside MBCA nesting window |
| on/≤500 m of **abandoned mine (AMIS)** | `soils_terrain`, `waste_hazmat`, `human_health` | contaminated-site assessment, hazard mgmt |
| in/near **floodplain / regulated area** | `surface_water`, `accidents_malfunctions` | flood-proofing, permit |
| **source water protection / wellhead / aquifer** area | `groundwater`, `human_health` | groundwater monitoring, prohibited activities |
| ≤ setback of **residence / noise receptor** | `noise_vibration`, `air_quality`, `light` | receptor limits, shadow flicker (wind) |
| within **treaty area / Indigenous territory** (always true in Canada) | `indigenous_rights_tluse` | engagement, TLU study, chance-find |
| **archaeological potential** (proximity to water/known sites) | `archaeology_heritage` | Stage 1–2 assessment, chance-find protocol |
| **prime agricultural land** (CLI 1–3 / specialty crop) | `socio_economic`, `soils_terrain` | ag-impact mitigation, drainage |
| **marine / large lake** waters | `fish_fish_habitat`, `wildlife_birds`, `noise_vibration` | underwater noise, marine timing |
| coincident with other approved/proposed projects (map density) | `cumulative_effects` | regional CE assessment |

Engine rules:
- `indigenous_rights_tluse` fires for **every** project (all Canadian land is
  subject to Aboriginal/treaty rights); it is a floor, never suppressed. Frame
  output as "communities to engage" using official Crown treaty layers only.
- `cumulative_effects` fires when ≥1 other project lies within the regional buffer.
- `closure_postclosure` fires by **archetype**, not spatial trigger (all mines,
  landfills, nuclear, wind/solar).
- A fired discipline with no extracted precedent still surfaces in the register as
  a flagged gap ("baseline constraint present; seek precedent conditions").
- Distances are conservative defaults; when an extracted condition supplies a real
  setback for the same archetype+discipline, prefer it and record provenance.

---

## Extension policy

New enum values (archetype, discipline, measure_type, plan_required) are added
here first, then in extraction code. Recurring unmapped text (≥3 occurrences)
should be reviewed for promotion out of `other`. Keep `measure_text` verbatim so
re-classification is always possible without re-fetching source PDFs.
