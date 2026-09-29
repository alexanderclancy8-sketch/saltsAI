---
title: Intruder and hold-up alarm systems — BS EN 50131, PD 6662, BS 8243, BS 9263 and police response
aliases: [intruder alarms, PD 6662, BS EN 50131, URN reference]
tags: [standards, intruder-alarms]
type: note
summary: Intruder alarm grading, the PD 6662 scheme, confirmed alarms, maintenance, police response and URNs, false alarm limits and ARC signalling.
created: 2026-09-28
updated: 2026-09-29
status: growing
related: ["[[cctv-and-access-control]]", "[[certification-and-competency]]", "[[maintenance-contracts-and-scheduling]]", "[[standards-moc]]"]
---

# Intruder and hold-up alarm systems: BS EN 50131, PD 6662, BS 8243, BS 9263 and police response

Summary: A UK guide to intruder alarm grading, the PD 6662 scheme, confirmed alarms (BS 8243), maintenance (BS 9263), police response and URNs, false alarm limits, and alarm signalling to alarm receiving centres (BS EN 50136).

Status: Written September 2026 from general industry knowledge. Police policy and signalling tables change, so anything marked **(verify)** should be checked against the current standard, the current NPCC policy or your certification body (NSI or SSAIB) before you advise a customer.

## Quick facts (most asked)

| Question | Answer |
|---|---|
| Main product/system standard | BS EN 50131 series (Part 1 = system requirements) |
| UK application document | PD 6662:2017. It sets how the EN 50131 series is applied in the UK **(verify for a newer edition)** |
| Confirmation standard (needed for police response) | BS 8243 (2021 edition) **(verify)** |
| Maintenance code of practice | BS 9263 (2016 edition) **(verify)** |
| Signalling standard | BS EN 50136 series (alarm transmission systems) |
| ARC standard | BS EN 50518 (monitoring and alarm receiving centres) |
| Grades | 1 (low risk) to 4 (high risk). Grade 2 for most homes and small commercial sites, Grade 3 for most commercial sites and higher-risk homes |
| Maintenance visits: remotely signalled systems | **Two preventive maintenance visits a year.** Under conditions, one of these may be a remote maintenance check **(verify)** |
| Maintenance visits: audible-only (bells-only) | **One visit a year** **(verify)** |
| Emergency call-out (monitored systems) | NSI/SSAIB rules typically expect attendance within **4 hours** for systems with remote signalling **(verify with your certification body)** |
| Police response requires | URN from the police force, an NSI/SSAIB-certificated installer, a compliant ARC, confirmation to BS 8243 and keyholders able to attend (commonly within 20 minutes) |
| False alarm limit (intruder) | **3 false alarms in a rolling 12 months** usually leads to police response being withdrawn. A warning usually follows the second **(verify current NPCC policy)** |

## Standards map

| Standard | What it covers |
|---|---|
| BS EN 50131-1 | System requirements: grading, detection, tamper, set/unset, notification |
| BS EN 50131-2-x | Detectors (PIR, dual-tech, door contacts, glass-break, etc.) |
| BS EN 50131-3 | Control and indicating equipment (panel) |
| BS EN 50131-4 | Warning devices (bells, sirens, strobes) |
| BS EN 50131-5-3 | Wire-free (radio) interconnections |
| BS EN 50131-6 | Power supplies |
| PD CLC/TS 50131-7 | Application guidelines |
| PD 6662 | UK scheme applying the EN 50131 family, plus UK requirements such as documentation and signalling |
| BS 8243 | Design of systems that generate **confirmed** alarms, and unsetting methods |
| BS 9263 | Commissioning, maintenance and remote support |
| BS EN 50136 series | Alarm transmission systems (ATS) and equipment |
| BS EN 50518 | Monitoring and alarm receiving centres |
| BS 7984 | Keyholding and response services |
| BS 7858 | Security screening (vetting) of staff |

## Security grades (BS EN 50131-1)

| Grade | Risk | Assumed intruder | Typical sites |
|---|---|---|---|
| 1 | Low | Little knowledge of alarm systems, limited range of easily available tools | DIY or basic systems. Generally not accepted for police response |
| 2 | Low to medium | Some knowledge of alarm systems, general tools and portable instruments (for example a multimeter) | Most homes, small shops and offices |
| 3 | Medium to high | Conversant with alarm systems, comprehensive tools and portable electronic equipment | Most commercial premises, high-value homes, pharmacies, jewellers (insurer-dependent) |
| 4 | High | Can plan the intrusion in detail and has a full range of equipment, including means to substitute components | Banks, high-security government and critical sites |

- The **system grade is the lowest grade of any component** in the detection and control chain. A Grade 3 panel with Grade 2 detectors is a Grade 2 system. The exception is where PD 6662 or the design explicitly allows lower-graded parts in areas that don't affect the overall grade **(verify)**.
- The grade should come from a documented risk assessment, which is often driven by the **insurer's requirement**. Record it in the design proposal and on the certificate.
- **Environmental classes:**
  - I: indoor, heated
  - II: indoor, general
  - III: outdoor, sheltered
  - IV: outdoor, general
  Match detectors and sounders to the location.
- Higher grades bring stricter requirements for tamper detection, anti-masking of detectors (Grade 3 and above), user authorisation codes, event log capacity, power supply monitoring and signalling path monitoring.

## Confirmed alarms: BS 8243

Police forces generally require new police-response systems to be able to generate a **confirmed** alarm, meaning there is evidence it is genuine before police are dispatched.

- **Sequential confirmation:** two separate detectors (independent, with non-overlapping fields of view) activate within the confirmation time window. The window is commonly 30–60 minutes **(verify)**. A single activation is an "unconfirmed" alarm. The ARC contacts keyholders but police aren't normally dispatched.
- **Audio confirmation:** ARC operators listen in to the premises.
- **Visual confirmation:** CCTV images or video clips are sent to the ARC for verification. This overlaps with BS 8418 detector-activated CCTV.
- **Hold-up alarms (HUA):** these have their own confirmation and design rules, including separate counting for false alarms.
- **Unsetting methods:** BS 8243 restricts how a system is unset so that a normal entry can't produce a confirmed alarm, and an intruder can't defeat confirmation by entering via the entry route. Typical permitted approaches:
  - unset before the entry door is opened (for example a remote fob or proximity reader outside)
  - opening the entry door disables confirmation
  - digital or proximity token unsetting
  - Check the list of permitted methods and their conditions in the current edition. **Don't rely on memory for which lettered method is which.**
- Confirmation design is one of the most common audit findings. Document the method chosen on the design proposal and the certificate.

## Police response, URNs and false alarm limits

**Policy basis.** The National Police Chiefs' Council (NPCC) publishes the policy on police requirements and response to security systems. It was formerly an ACPO policy. Each force applies it through its alarms administration office (West Yorkshire Police for Salts' area). **Check the current version before advising customers.**

**Types of system:**

| Type | Description | Police response |
|---|---|---|
| **Type A** (remote signalling, URN) | Installed and maintained by an NSI/SSAIB-certificated company to the relevant standards. Monitored by a compliant ARC. Confirmation to BS 8243 | Police response on confirmed alarms, once a **URN** is issued |
| **Type B** (non-URN) | Bells-only, self-monitored (app or speech dialler), or not to standard | No automatic response. Police attend only with evidence of a crime in progress (for example a witness report) via 999 |

**URN (unique reference number):**
- Issued by the police force when the certificated installer applies after installation.
- Requirements: compliance certificate, ARC details, and at least two trained keyholders (or a BS 7984 keyholding company) who can attend within about 20 minutes **(verify)**.
- Forces charge an administration fee per URN **(verify local fee)**.
- On a change of installer or ARC, the URN must be transferred or updated with the force. Don't let it lapse.

**Response levels:**
- **Level 1:** immediate or emergency response.
- **Response withdrawn:** commonly called "Level 3". Police don't respond to activations.

**False alarm limits (intruder, commonly applied — verify):**
- **2 false calls in a rolling 12 months:** warning letter to the customer.
- **3 false calls in a rolling 12 months:** response withdrawn.
- **Reinstatement:** normally needs written confirmation of remedial work from the installer, then a period free of false calls (commonly 3 months).
- Hold-up alarm false calls are counted separately, with their own limit. Check it.

**Preventing false calls:**
- User training on set/unset.
- Correct entry/exit routes and timers.
- Check detector siting: heaters, pets, curtains, insects, spiders in PIRs, balloons.
- Good door contact alignment.
- Proper battery and PSU maintenance.
- Confirmation working correctly.
- Prompt engineer visits after any unconfirmed activation.

## Signalling and ARC paths (BS EN 50136)

**Alarm transmission system (ATS) categories:**
- **Single path (SP1–SP6)** and **dual path (DP1–DP4)** categories under BS EN 50136-1.
- A higher number means shorter maximum transmission times and more frequent path monitoring, so a lost path is noticed faster.
- EN 50131-1 and PD 6662 set a **minimum ATS category for each grade** and notification option. Look up the current table rather than quoting from memory **(verify)**.

**Typical signalling paths (described generically):**

| Path | Notes |
|---|---|
| **Dual-path signaller (IP + cellular)** | The common modern choice. One path over the customer's broadband or network, the other over a mobile network (4G/LTE). Both are monitored, and a path failure raises a fault at the ARC. Several UK providers offer these, and brand names are often used loosely for the whole category |
| Single-path IP or single-path cellular | Lower categories. Acceptable for some lower-grade systems if the category meets the requirement |
| Legacy PSTN digital communicator / speech dialler | **Being made obsolete by the analogue phone switch-off.** Openreach/BT's PSTN migration to digital voice (the national deadline was extended to January 2027 **(verify)**) makes analogue diallers unreliable. Migrate them to IP/cellular now |
| Legacy dedicated line / PSTN-based dual path | Also affected by the PSTN switch-off. Check the provider's migration plan |

**Other signalling points:**
- **Cellular network sunsets:** UK operators have switched off 3G. 2G is expected to end by 2033 at the latest **(verify each network's date)**. Audit signallers that depend on 2G/3G and plan replacements as a sales opportunity.
- **Site network changes** often cause IP path failures: new routers, firewall changes, ISP swaps. Tell customers to contact you before changing broadband.
- **ARC:** must meet BS EN 50518 and be accepted by the police for URN systems. The ARC holds keyholder lists and response instructions and passes confirmed alarms to police.
- Signal types to agree with the ARC: fire (if combined), intruder unconfirmed and confirmed, HUA, tamper, set/unset (open/close) reporting, mains fail, low battery, path fault, test timer.

## Maintenance: BS 9263 and certification body rules

| System type | Preventive maintenance (typical) | Notes |
|---|---|---|
| Remotely signalled (ARC, police or keyholder response) | **2 visits per year** | Under BS 9263 and NSI/SSAIB rules, one visit may be replaced by remote maintenance where the system supports it and conditions are met **(verify)** |
| Audible-only (bells-only) | **1 visit per year** | |
| Hold-up alarm systems | As for the intruder system they form part of | Test every HUA device at each visit (with ARC on test) |
| Wire-free systems | As above, plus check signal strength and device batteries | Many manufacturers recommend battery replacement cycles. Record battery dates |

**A preventive maintenance visit typically includes:**
1. Review the event log and false alarm history since the last visit. Discuss with the customer.
2. Put the system on test with the ARC.
3. Walk-test every detector, including checks for masking, obstruction and range. Check door contacts, shock sensors, glass-break, beams and any anti-masking functions.
4. Test every hold-up device with the ARC.
5. Test tamper circuits on the panel, detectors, junction boxes and the bell box.
6. Test warning devices: internal sounder, external bell or strobe (including the self-actuating battery in the bell box).
7. Test the PSU and batteries: mains fail, battery load and capacity, charger voltage. Replace batteries at end of life (sealed lead-acid typically 3–5 years depending on the environment) **(verify manufacturer)**.
8. Test signalling: every signal type received correctly at the ARC, both paths on dual-path, path fault reporting.
9. Check user codes, set/unset method (including BS 8243 compliance), entry/exit times, and the date and time.
10. Update records, issue a maintenance certificate or report, list recommendations and note any change of risk that may require an upgrade.

**Corrective maintenance:** respond to faults and activations within the contract SLA. For police-response systems, certification bodies expect 24/7 availability and short attendance times (commonly 4 hours) **(verify)**.

**Remote support:** remote access to panels (via app or upload/download software) must be secure and authorised by the customer. Log sessions, and don't leave default engineer codes in place.

## Documentation to issue (typical)

- A design proposal / system specification. It should state the grade, environmental class, notification option, signalling category, confirmation method and unsetting method.
- A **certificate of installation** or compliance (NSI or SSAIB format) confirming the standards met.
- The URN application form (for police response) and the ARC setup form.
- Handover documentation: user instructions, zone list, user training record and maintenance arrangements.
- Maintenance records and certificates after each visit.
- Staff vetted to **BS 7858** where they access security system information. This is a certification body requirement.

## Common faults and triage

| Fault / symptom | Likely cause | First checks |
|---|---|---|
| Repeated false alarm from one PIR | Heat source, draught, insect, pet, sunlight, curtain movement, failing detector | Check the event log for zone and time. Check the environment. Consider a dual-tech or pet-tolerant detector, or re-site |
| Tamper fault | Lid not closed, cable damage, bell box tamper, water ingress | Check the event log for which tamper. Inspect junction boxes and the external sounder |
| AC / mains fail | Fused spur off, site power work | Check the spur and label it "ALARM – DO NOT SWITCH OFF" |
| Low battery / battery fault | Battery aged, charger fault, long mains outage | Test and replace batteries. Check charger output |
| ATS / path fault | Router change, broadband outage, SIM or coverage problem, signaller PSU | Check the signaller LEDs and portal. Liaise with the signalling provider and ARC |
| Set failure / zone won't set | Door open, detector faulted, anti-mask triggered | Check which zone. Anti-mask needs clearing on site at Grade 3 |
| Wireless device supervision loss | Flat battery, interference, device moved | Check signal strength history. Replace battery. Re-site the receiver or expander |

## Insurance and sales points

- Insurers often specify the grade, NSI/SSAIB-certificated installer, signalling category and police response. Get the requirement in writing before quoting.
- A lapsed maintenance contract, or lost police response, can invalidate insurance cover. Remind customers at renewal.
- Upgrade drivers:
  - PSTN switch-off
  - 2G/3G sunset
  - end-of-life panels (no firmware support)
  - customers after a burglary
  - insurer changes
  - adding video verification

## Related files

- [[cctv-and-access-control]] (BS 8418, video verification)
- [[certification-and-competency]] (NSI, SSAIB, BS 7858)
- [[maintenance-contracts-and-scheduling]]
