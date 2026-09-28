# Fire detection and fire alarm systems: BS 5839-1:2025, BS 5839-6 and BS EN 54

Summary: A practical UK guide to fire alarm system categories, design basics, servicing intervals, certificates, false alarm management and fault-finding, covering BS 5839-1 (non-domestic), BS 5839-6 (domestic) and the BS EN 54 product standards.

Status: Written September 2026 from general industry knowledge. It is not a clause-by-clause reading of the purchased standards, so anything marked **(verify)** needs checking against the current edition before you quote it to a client, auditor or fire safety officer. We don't give clause numbers because they change between editions.

## Quick facts (most asked)

| Question | Answer |
|---|---|
| Current non-domestic code of practice | BS 5839-1:2025. It superseded BS 5839-1:2017 (+A1 where applicable). Check the date the 2017 edition was formally withdrawn **(verify)** |
| How often must a system be serviced? | Inspection and servicing visits at intervals **not exceeding 6 months** |
| What is the "5–7 month window"? | Industry and auditors accept a visit made 5 to 7 months after the previous one as meeting the six-monthly recommendation. Visits further apart than that mean the system is not being maintained to the standard **(verify exact wording in 2025)** |
| Must every device be tested? | Yes. Every manual call point (MCP) and automatic detector should be functionally tested at least once every 12 months, so usually about half at each six-monthly visit (or a quarter at each quarterly visit) |
| User test | **Weekly.** Operate an MCP, using a different one each week in rotation, at about the same time during working hours. Record the result in the logbook |
| Standby battery duration (typical) | 24 h standby followed by 30 min of full alarm load. Allow longer where a fault might not be noticed, for example in premises left unoccupied **(verify)** |
| Battery life | Sealed lead-acid batteries usually last 4–5 years by design. Replace them per the manufacturer's advice and date-label them |
| Smoke detector coverage (flat ceiling) | Radius 7.5 m **(2017 figure, verify 2025)** |
| Heat detector coverage (flat ceiling) | Radius 5.3 m **(2017 figure, verify 2025)** |
| Sound levels | 65 dB(A) generally, or 5 dB(A) above persistent background noise. 60 dB(A) is accepted in stairways and very small rooms. 75 dB(A) at the bedhead where sleepers must be woken. Maximum 120 dB(A) **(2017 figures, verify)** |
| MCP travel distance | Nobody should travel more than 45 m to reach an MCP. Reduce this to 25 m where occupants have limited mobility or fire could develop rapidly **(verify)** |
| MCP mounting height | About 1.4 m above floor level. Lower is acceptable where wheelchair users are likely to be the first to raise the alarm |
| Zone limits | Max 2,000 m² floor area per zone. Max 60 m search distance. One storey per zone unless the whole building is 300 m² or less |
| Loop isolation | Place short-circuit isolators so one fault cannot disable more than one zone. BS EN 54-2 limits the devices between isolators, commonly quoted as 32 **(verify)** |
| Domestic standard | BS 5839-6:2019+A1:2020. Check whether a newer edition has been published **(verify)** |

## The 2025 edition: what we know and what to verify

- BS 5839-1:2025 replaced the 2017 edition. Most of the core structure is unchanged: categories, the maintenance regime, the certificate set and the variation concept.
- Industry commentary has reported the changes below. **We have not verified these against the text, so treat them as "check before relying on":**
  - Stronger recommendations where people sleep, including restrictions on relying on heat detectors in sleeping rooms.
  - Updated guidance on transmitting alarm signals to an alarm receiving centre (ARC), and on reducing unwanted fire signals.
  - Clarified wording on variations, on documentation handed over (including cause and effect), and on the responsibilities of each party.
  - Updated references to current product standards, cable test standards and post-Building Safety Act terminology.
- A system designed and installed to the 2017 (or earlier) edition doesn't have to be upgraded just because a new edition has been published. It is normal practice to maintain it to the current edition's servicing recommendations and to report significant shortfalls to the responsible person as recommendations **(general industry practice, verify with your certification body)**.
- Where the 2017 and 2025 guidance differ, state on certificates which edition the design was assessed against.

## System categories (BS 5839-1)

| Category | Purpose | Coverage |
|---|---|---|
| **M** | Life safety, manual only | Manual call points only, no automatic detection. Relies on people discovering the fire |
| **L1** | Life safety, maximum | Automatic detection throughout all areas of the building, with only minor exceptions **(check the exceptions list)** |
| **L2** | Life safety, enhanced | Everything in L3, plus detection in defined higher-risk rooms or areas, or where occupants are at particular risk |
| **L3** | Life safety, escape routes | Detection on escape routes and in rooms opening onto escape routes, to warn people before routes become impassable |
| **L4** | Life safety, circulation only | Detection within the circulation areas that form escape routes (corridors, stairways) |
| **L5** | Life safety, engineered | Bespoke coverage to meet a specific fire engineering objective or a localised risk. It needs a clear written specification |
| **P1** | Property protection, full | Automatic detection throughout the building |
| **P2** | Property protection, partial | Detection only in defined parts, such as high-value or high-risk areas |

- Categories are often combined, for example "L2/P2". Nearly all L and P systems also have manual call points.
- The fire risk assessment, building regulations approval, fire engineer or insurer usually sets the category. The designer should record who specified it.

## Design essentials (summary, not a design guide)

- **Detector siting (2017 figures, verify):** place smoke detector sensing elements roughly 25–600 mm below the ceiling and heat detectors roughly 25–150 mm below. Keep detectors at least 500 mm from walls and partitions, keep 500 mm clear of obstructions and storage, and avoid siting within 1 m of air supply vents.
- **Ceiling height limits (2017, verify):** smoke detectors about 10.5 m, Class A1 heat detectors about 9 m, other heat classes about 7.5 m. Taller spaces need beam detectors, aspirating systems or flame detection.
- **Voids:** voids deeper than about 800 mm generally need detection in L1/P1 systems **(verify)**.
- **Detector choice:** use multi-sensor detectors (optical plus heat) where unwanted alarms from cooking fumes or steam are likely. Carbon monoxide fire detectors (BS EN 54-26) suit some sleeping risks. Heat detectors go in kitchens and plant rooms, but see the 2025 sleeping-room caution above.
- **Sounders and visual alarm devices (VADs):** meet the audibility levels in the quick facts. Specify VADs to BS EN 54-23 (categories C ceiling, W wall, O open class, with a stated coverage volume) where hearing-impaired people may be alone or background noise is high.
- **Power supplies:** use a dedicated mains circuit with a double-pole isolating device, labelled "FIRE ALARM – DO NOT SWITCH OFF" and secured against unauthorised operation. Avoid RCD protection shared with other circuits **(verify current wording)**. Size the batteries using the standard's battery calculation, which applies an ageing/de-rating factor. Manufacturers' calculators do this for you.
- **Remote monitoring:** remote transmission of alarm signals to an ARC is strongly recommended for care premises where residents sleep and may need assistance. Check what the fire risk assessment and fire strategy require.

## Cause and effect

A cause and effect matrix documents exactly what each input (detector, MCP, zone, sprinkler flow switch, suppression signal) does to each output.

| Input (cause) | Typical outputs (effects) |
|---|---|
| Any detector or MCP, any zone (single-stage) | All sounders and VADs operate. ARC signal sent. Door hold-open devices release. Maglocks on escape doors release. Lifts home to the fire exit level (BS EN 81-73). Air handling units shut down |
| Phased or staged evacuation building | "Evacuate" signal on fire floor (and floor above), "alert" signal elsewhere. Timed or manual escalation |
| Two detectors in the same zone (coincidence or "double knock") | Gas suppression release sequence, pre-action sprinkler valve, or other high-consequence action |
| Sprinkler flow switch | Fire condition for that zone. The valve monitoring (tamper) switch is treated as a fault or "supervisory" signal, not a fire |
| Kitchen suppression discharged | Fire condition. Gas supply shut-off confirmed |
| Smoke vent / AOV zone | Opens the vents serving the fire floor or stair |

- Agree it at design stage with the client, fire engineer, building control and other trades such as mechanical, lifts and security.
- Programme it, then test every line at commissioning with witnesses. Retest after any modification.
- Keep the signed matrix in the logbook or O&M file. Engineers need it on service visits to prove interfaces still work.
- Isolate irreversible outputs (gas release, plant shutdown in critical processes) before testing detection.

## Zone plans and zoning

- Display a zone plan or diagram next to the control and indicating equipment (CIE) and at any repeater panels. It should be oriented to match the building, with "you are here", the CIE location, entrances and zone boundaries. Fire and rescue services rely on it, and auditors check for it.
- An addressable system with text descriptions still needs zoning and a zone plan. Make the device location text meaningful to a firefighter (for example "Ground floor corridor outside kitchen"), not engineering codes.
- Zoning rules (2017, verify): max 2,000 m² per zone, 60 m search distance, one storey per zone unless the building total is 300 m² or less. Stairwells, lift shafts and other vertical shafts form their own zones.
- Update the zone plan and device text whenever the building layout changes.

## Cables

| Cable performance | Typical test basis | Where used |
|---|---|---|
| **Standard** fire-resisting | PH30 classification to BS EN 50200 plus the 30-min survival test with water spray (BS EN 50200 Annex E) **(verify)** | Most buildings |
| **Enhanced** fire-resisting | PH120 to BS EN 50200 plus the 120-min test to BS 8434-2 **(verify)** | Higher-risk or taller buildings (see below) |

- The 2017 edition expected all critical fire alarm wiring to be fire-resisting. That covers detection loops, sounder circuits, mains supply to the CIE and links to other panels.
- Enhanced cable was typically needed in these cases (2017 triggers, verify for 2025):
  - unsprinklered buildings over 30 m high
  - unsprinklered buildings using phased evacuation in four or more phases
  - where cables must keep working to serve areas people remain in while fire affects another part of the building
  - where the fire strategy or risk assessment calls for it
- Common cable types:
  - soft-skin cables to BS 7629-1 (the FP200-type family)
  - fire-resistant armoured cable to BS 7846
  - mineral-insulated cable to BS EN 60702-1
- Use a single uniform colour, preferably red, and don't use that colour for other services.
- Support cables with fire-resistant (metal) fixings so they won't collapse early in a fire. BS 7671 (18th Edition) requires this for all wiring systems. Plastic clips or plastic trunking alone are not acceptable support.
- Segregate cables from other services, minimise joints (label any junction boxes) and fire-stop penetrations through compartment walls and floors.
- Installation tests: insulation resistance is normally required to be at least 2 MΩ at 500 V dc, between conductors and to earth **(verify)**. Always disconnect devices and panel first. Also record continuity and screen/earth continuity.

## Responsibilities and certificates

| Role | Responsibility | Certificate |
|---|---|---|
| Designer | Category, detector and sounder layout, zoning, cause and effect, cable grade, battery calculation, variations | Design certificate |
| Installer | Installation to the design and the standard's installation recommendations, cable testing | Installation certificate |
| Commissioner | Full functional test, sound-level readings, cause and effect proved, documentation complete | Commissioning certificate |
| Responsible person / user | Accepts the system, keeps the logbook, runs weekly tests, arranges servicing, manages false alarms | Acceptance certificate |
| Third-party verifier (optional) | Independent check of the design and installation | Verification certificate |
| Maintainer | Periodic inspection and servicing, reporting defects | Inspection and servicing certificate (each visit) |
| Anyone altering the system | Modifications to the standard | Modification certificate |

- One firm may hold several roles, but each certificate should be signed by a competent person for that stage.
- BAFE SP203-1 certificated firms also issue a BAFE Certificate of Compliance for the modules they performed (design, installation, commissioning/handover, maintenance).
- The handover pack should include:
  - all certificates
  - as-fitted drawings
  - the zone plan
  - the cause and effect matrix
  - the battery calculation
  - the logbook
  - O&M manuals
  - user training records
  - the list of agreed variations

## Variations

- A variation is a deliberate departure from the standard's recommendations, agreed between the interested parties, justified and **recorded on the relevant certificate**. Typical parties are the designer, client or responsible person, and where relevant building control, the fire risk assessor or the insurer.
- Variations are not a way to record defects or unfinished work. Those are non-compliances and need fixing.
- The 2017 edition introduced a list of matters that should **not** be accepted as variations. Check the current list in the 2025 edition rather than relying on memory **(verify)**.
- Good practice: number each variation, give the reason and who agreed it, and carry it forward on every service certificate so it isn't lost.

## Logbook

The responsible person keeps it, on site, near the CIE. It should record:
- the name of the person responsible for the system, the servicing organisation and the ARC
- every fire alarm signal, with the cause (real fire, unwanted alarm, equipment false alarm, malicious, false alarm with good intent)
- every fault, disablement and isolation, with times and who authorised it
- weekly tests, service visits, modifications and remedial works
- the device or MCP used in each weekly test (so rotation can be proved)

## User testing and routine checks

- **Weekly:**
  - Operate an MCP (a different one each week) during normal working hours, at roughly the same time.
  - Check the panel receives the signal and all sounders operate.
  - Notify the ARC first if the system is monitored, then reset and record.
  - Keep the sound short so occupants learn to recognise a test.
- **Shift workers:** add a periodic test at a time when staff who never hear the weekly test are present **(verify the recommended frequency)**.
- **Monthly (where applicable):**
  - Start any standby generator that supports the fire alarm and check it takes the load.
  - Check vented batteries if fitted (rare).
- **Daily (good practice, larger sites):** check the panel shows normal status and that any faults logged have been acted on.

## What a service visit (periodic inspection and test) includes

Checklist based on the standard's recommendations (confirm details against the current edition):
1. Read the logbook. Review faults, disablements and **false alarms since the last visit**. Work out the false alarm rate and discuss it with the responsible person.
2. Walk the building visually. Look for changes in structure, use, partitions, storage or occupancy that affect detection or audibility, such as new rooms without detectors or racking near detectors.
3. Check detectors are unobstructed, not painted over, not covered (dust covers left on after works) and not missing.
4. Test every CIE fault indication by simulating faults: open and short circuit, earth, mains fail, battery disconnect.
5. **Batteries and charger:** inspect, check connections, measure charger voltage and test battery condition with suitable equipment (not just terminal voltage). Record voltages and battery date. Replace at end of life.
6. Functional tests:
   - MCPs and detectors on a rota, so every device is tested within 12 months.
   - Use the right stimulus: approved smoke aerosol for optical detectors, a heat source for heat detectors (never a naked flame), and the manufacturer's method for multi-sensor, CO, beam and aspirating devices.
   - Check the correct location or zone text is displayed.
7. Check sounders and VADs operate. Carry out spot audibility checks where changes have occurred.
8. **Check cause and effect and interfaces:** door releases, maglock release, plant shutdown, lift homing, smoke vents, suppression signals (safely isolated), and ARC fire and fault signals received at the ARC.
9. Check the printer, networked and repeater panels, radio devices (signal strength and battery), and remote signalling paths.
10. Record everything and issue an inspection and servicing certificate. Report defects in writing to the responsible person, with urgency. Update the logbook.

- **Non-routine attention** (outside the six-monthly cycle) may be needed after a fire, after repeated false alarms, after building work or alterations, or after a long disablement.
- **Special inspection on takeover:** when a new maintenance organisation takes over a system, the standard recommends a special inspection first. It checks documentation and design basics, not just a routine test **(verify scope)**. Price this into new contract takeovers.
- Quarterly visits are common on large sites. Each visit tests about a quarter of the devices.
- Many panels keep an event log. Download or review it at each visit.

## False alarm management

| Type | Meaning | Examples |
|---|---|---|
| Unwanted alarm | Detector responded to fire-like phenomena | Toast, cooking fumes, steam from showers, aerosols, dust from building work, vape or smoke |
| Equipment false alarm | Fault in the system caused a fire signal | Faulty detector, water ingress, wiring fault |
| Malicious false alarm | Deliberate | Broken MCP with no fire |
| False alarm with good intent | Someone genuinely thought there was a fire | MCP operated for a burning smell |

- The servicing organisation should review the false alarm record at every visit and give the responsible person advice to reduce it. Commonly used triggers for investigation (2017-era figures, **verify**) are:
  - two or more false alarms in a four-week period, or
  - an annual rate above roughly one per 100 automatic detectors.
- Remedies:
  - relocate the detector, or change its type (multi-sensor, heat where appropriate)
  - use covers or temporary isolation during dusty works, with a permit and a fire watch, and remove them afterwards
  - fit MCP protective covers
  - train staff on cooking and extraction
  - use coincidence detection
  - use staff alarm or investigation delays where the fire strategy permits
  - agree ARC call filtering
- Fire and rescue services (including in West Yorkshire) have policies on attending automatic fire alarm calls. These may reduce or remove attendance to unconfirmed AFA calls from some premises types. Check the current local policy rather than assuming attendance.
- Repeated unwanted fire signals cost the client in disruption and possible charges, and undermine confidence. Log the cause of every alarm.

## BS 5839-6: domestic premises

**Grades** (2019 edition; Grade B was removed in 2019 **(verify current edition)**):

| Grade | Summary |
|---|---|
| A | Full system with BS EN 54 CIE, detectors, sounders and MCPs (close to BS 5839-1 practice) |
| C | Detectors and sounders (may be smoke alarms) with central control equipment and a common standby power supply |
| D1 | Mains-powered smoke/heat alarms with a **tamper-proof** integral standby supply (for example a sealed long-life lithium cell) |
| D2 | Mains-powered alarms with a **user-replaceable** standby battery |
| E | Mains-powered alarms with no standby supply **(verify)** |
| F1 | Battery-only alarms with tamper-proof long-life battery |
| F2 | Battery-only alarms with user-replaceable battery |

**Categories:**
- **LD1:** detection in all circulation spaces on escape routes and all rooms where fire might start (excluding bathrooms and toilets).
- **LD2:** circulation spaces plus specified rooms, typically the kitchen and the principal habitable room.
- **LD3:** circulation spaces on escape routes only.
- **PD1 and PD2:** property protection equivalents.

**Legal minimums (verify current rules):**
- **England new-build (Approved Document B):** at least Grade D2 Category LD3. BS 5839-6 itself recommends higher levels for many dwellings.
- **England rented homes:** a smoke alarm on each storey with living accommodation, and a CO alarm in rooms with a fixed combustion appliance (excluding gas cookers). These come from the Smoke and Carbon Monoxide Alarm (England) Regulations 2015 as amended from October 2022.
- **Scotland (since Feb 2022, all homes):**
  - interlinked smoke alarms in the living room and every circulation space
  - an interlinked heat alarm in the kitchen
  - a CO alarm where there is a fuel-burning appliance or flue
  - alarms either sealed long-life battery or mains-powered

**Other domestic points:**
- HMOs and blocks of flats often need Grade A or D1 systems depending on layout. Take guidance from the fire risk assessment and local housing authority.
- Advise users to test regularly as the manufacturer instructs, and to replace alarms at their end-of-life date (typically 10 years).

## BS EN 54 product standards (overview)

| Part | Covers |
|---|---|
| EN 54-1 | Introduction and definitions |
| EN 54-2 | Control and indicating equipment (CIE / panel) |
| EN 54-3 | Fire alarm sounders |
| EN 54-4 | Power supply equipment |
| EN 54-5 | Point heat detectors |
| EN 54-7 | Point smoke detectors |
| EN 54-10 | Flame detectors |
| EN 54-11 | Manual call points |
| EN 54-12 | Optical beam (line) smoke detectors |
| EN 54-13 | Compatibility assessment of system components |
| EN 54-16 | Voice alarm control and indicating equipment |
| EN 54-17 | Short-circuit isolators |
| EN 54-18 | Input/output devices (interfaces) |
| EN 54-20 | Aspirating smoke detectors |
| EN 54-21 | Alarm transmission and fault warning routing equipment |
| EN 54-22 | Resettable line-type heat detectors |
| EN 54-23 | Visual alarm devices (VADs) |
| EN 54-24 | Voice alarm loudspeakers |
| EN 54-25 | Radio (wireless) link components |
| EN 54-26 | Carbon monoxide point fire detectors |
| EN 54-27 | Duct smoke detectors |
| EN 54-29 / -30 / -31 | Multi-sensor detectors (smoke+heat, CO+heat, smoke+CO(+heat)) |

- Products should carry the required conformity marking. UKCA/CE recognition rules for construction products have changed several times, so **verify the current GB position**. Ideally they also carry third-party approval (for example LPCB, VdS or BRE listings).
- Only mix devices from different manufacturers on the same loop where the panel manufacturer confirms compatibility (EN 54-13). Otherwise you risk invalidating approvals.

## Common panel and device brands (UK)

| Brand | Notes |
|---|---|
| Gent (Honeywell) — Vigilon, Nexus/S-Quad devices | Proprietary Gent loop protocol, so Gent devices are needed on Gent loops. Widespread in larger commercial, education and healthcare sites |
| Kentec — Syncro AS, Taktis, Sigma (conventional) | Addressable panels on Apollo or Hochiki protocol variants. Check which protocol before ordering devices |
| Advanced (Advanced Electronics) — MxPro 5 | Multi-protocol panel (Apollo, Hochiki and others depending on loop card). Common on networked sites |
| Apollo | Detector and device manufacturer (XP95, Discovery, Soteria families). XP95/Discovery use XPERT address cards in the base **(verify for Soteria)** |
| Hochiki | Detectors (ESP protocol) and panels. Addressing by hand programmer |
| C-TEC | Popular conventional (CFP) and addressable panels, plus ancillaries |
| Morley-IAS, Notifier (Honeywell) | Addressable systems in commercial buildings |
| Others met in the field | Ziton, Cooper, Haes, Hyfire (wireless), EMS (wireless), Siemens, Detectomat |

Always use the manufacturer's manual, configuration software and training. Engineer access codes and programming tools vary.

## Fault-finding triage (typical faults engineers see)

| Panel message | Likely causes | First checks |
|---|---|---|
| **Earth fault** | Damaged insulation (screw or nail through cable), water in external or basement devices, cable screen touching a metal back box, crushed glands, other trades' works | Check the event log time (after rain? after contractors?). Disconnect circuits one at a time to find which clears it. Check external sounders and damp areas first. Check screen termination and continuity. Insulation-test only with devices disconnected. Use split-half fault location |
| **Loop open circuit** | Broken cable, loose terminal in a base, device removed, termination disturbed during works | Addressable loops keep working from both ends, but redundancy is lost, so fix promptly. Panel diagnostics show the last device responding from each end. Inspect between them |
| **Loop short circuit / isolator operated** | Crushed or trapped cable, water, wiring error, failed device | Devices between the two operated isolators are lost, so treat as urgent. Locate the isolator pair, split the section and check bases |
| **Device missing / not responding** | Detector head removed (decorators), device failed, address clash, contamination, wiring fault | One device: re-seat, check address, replace. **Several consecutive devices missing** suggests wiring or an isolator. Check for double addressing after replacements |
| **Wrong device type / unexpected device** | A replacement of a different type or protocol fitted, or a wrong address set | Match the device type and protocol to the panel configuration. Update configuration if the change is intended |
| **Detector dirty / drift / maintenance alert** | Contamination, compensation limit reached | Clean or replace the detector. Check the environment (dust, kitchens) |
| **Battery fault** | Batteries at end of life, high internal resistance, loose link or fuse, charger fault, wrong capacity | Check battery date. Measure charger output against the manufacturer's figure. Test batteries with a battery tester. Replace as a matched pair, then re-check the battery calculation |
| **Mains / PSU fault** | Supply isolator off, fuse or MCB tripped, remote PSU failure (EN 54-4), blown aux fuse | Check the labelled isolator and upstream protection. Check remote PSUs and their batteries too |
| **Sounder circuit fault** | Missing or wrong EOL (conventional), shorted sounder, damaged external sounder | Check the EOL value per the panel manual. Isolate sounders to find the fault |
| **Network / comms fault** | Panel-to-panel or repeater link broken, card fault | Check network cabling and termination. Check the other panel is powered |
| **ARC / signalling fault** | IP or radio path failure, signalling unit power or battery fault | Check the signaller status, confirm with the ARC, and check network/router changes on site |
| **Conventional zone open / short** | EOL device missing, cable fault, detector base wiring | Everything beyond an open circuit is unmonitored, so treat as urgent |

**Triage tips:**
- Before attending, ask the site to read the exact panel text, the number of faults and whether any sounders or fire LEDs are on. Photos of the display help.
- Look for patterns in the event log: time of day, weather, cleaning or contractor activity.
- Never leave a system disabled without the responsible person's written knowledge. Advise interim measures such as a fire watch and a fire risk assessment review.
- Record every fault and fix in the logbook and on the job sheet. Repeated faults on one device or circuit should trigger a root-cause remedial quote.

## Safe working on live systems

- Tell the site contact and the ARC before testing. Put the system "on test" at the ARC and confirm afterwards that it's back to normal.
- Disable or isolate outputs that shouldn't operate during tests: gas or suppression release, plant shutdown in critical processes, lift homing in busy buildings. **Isolate extinguishing system release before testing detection in protected rooms.**
- Use the panel's disablement functions rather than disconnecting wires where possible, and log every disablement.
- Test aerosols and heat tools: follow the manufacturer's instructions and COSHH data. Never use a naked flame.
- Working at height, asbestos (pre-2000 buildings) and electrical safety rules all apply. See `legislation/certification-and-competency.md`.

## Related files

- `standards/emergency-lighting-bs5266.md`
- `standards/fire-extinguishers-and-other.md` (voice alarm, EVC, aspirating, door holders, suppression interfaces)
- `legislation/uk-fire-safety-law.md`
- `operations/maintenance-contracts-and-scheduling.md`
