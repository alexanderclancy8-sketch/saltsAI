# CCTV and access control: BS EN 62676, BS 8418, UK GDPR, BS EN 60839-11 and BS 7273-4

Summary: A UK guide to specifying, installing and maintaining CCTV and access control systems. Covers the BS EN 62676 video surveillance series, BS 8418 remotely monitored detector-activated CCTV, UK GDPR/ICO obligations, the Surveillance Camera Code, BS EN 60839-11 access control, fail-safe vs fail-secure locking and the fire alarm interface for maglocks (BS 7273-4).

Status: Written September 2026 from general knowledge. Data protection law was amended by the Data (Use and Access) Act 2025, so check current ICO guidance. Anything marked **(verify)** should be checked against the current edition or guidance.

## Quick facts (most asked)

| Question | Answer |
|---|---|
| Video surveillance system standard | BS EN 62676 series (Part 4 = application guidelines) |
| Remotely monitored, detector-activated CCTV | BS 8418 (2021 edition) **(verify)** |
| Access control standard | BS EN 60839-11-1 (system and component requirements, grades 1–4) and BS EN 60839-11-2 (application guidelines) |
| Door release on fire alarm | BS 7273-4 (actuation of release mechanisms for doors), 2015 edition as amended **(verify)** |
| Maglocks on escape routes | Must be **fail-safe**. Release on fire alarm, on power failure and by a green emergency door release unit. Follow the fire risk assessment |
| CCTV maintenance (typical) | At least **annually**. Remotely monitored BS 8418 systems usually **twice a year** **(verify)** |
| Access control maintenance (typical) | At least **annually**. Every 6 months for critical or high-use sites |
| DORI pixel densities (BS EN 62676-4) | Detect 25 px/m, Observe 62 px/m, Recognise 125 px/m, Identify 250 px/m |
| Subject access request deadline | One month, extendable by up to two further months for complex requests |
| Footage retention | No fixed legal period. Keep only as long as necessary. About 30 days is common, but it must be justified |

## CCTV standards map

| Standard / document | Covers |
|---|---|
| BS EN 62676-1-1 | System requirements (security grades 1–4, similar concept to EN 50131) |
| BS EN 62676-1-2 | Video transmission performance requirements |
| BS EN 62676-2-x | IP video interoperability and protocols |
| BS EN 62676-3 | Analogue and digital video interfaces |
| BS EN 62676-4 | Application guidelines: operational requirement, DORI, commissioning, documentation, maintenance |
| BS 8418 | Detector-activated remotely monitored systems (installation and remote monitoring) |
| BS 7958 | Management and operation of CCTV (control rooms) |
| BS EN 50518 | Monitoring and alarm receiving centres (also covers remote video response centres) |
| NSI / SSAIB codes of practice | Certification body requirements for CCTV firms (confirm the code number and scope with the body) |

## Operational requirement (OR) and image quality

- Start every design with a written **operational requirement**:
  - what is being protected and why (the threats)
  - what the camera must achieve at each location
  - when it's needed (day or night)
  - who watches, and how they respond
  - retention and export needs
- **DORI** (BS EN 62676-4) sets the target pixel density at the target distance:

| Purpose | Pixels per metre (horizontal) | What it means |
|---|---|---|
| Detect | 25 | Tell whether a person or vehicle is present |
| Observe | 62 | See characteristic details such as clothing and activity |
| Recognise | 125 | Recognise a known individual with a high degree of certainty |
| Identify | 250 | Identify an unknown person beyond reasonable doubt |

- Number plate capture and ANPR need specific camera and illumination design. Follow the ANPR camera manufacturer's guidance.
- Consider lighting (IR range, white light, wide dynamic range for backlit doors), mounting height, vandal resistance (IK rating), environmental rating (IP rating) and cable routes.
- Commission against the OR. Record the field of view for each camera (a reference image) so later maintenance can prove nothing has moved.

## BS 8418: detector-activated remotely monitored CCTV

- **What it is:** detectors (PIR, beams, video analytics) trigger alarm events. The system sends video to a **remote video response centre (RVRC)**, usually part of a BS EN 50518 ARC. Operators verify the event, use audio challenge (loudspeakers) and call police or keyholders.
- **Common uses:** construction sites, vacant properties, car dealerships, yards, depots, unmanned sites.
- **Police response:** a compliant BS 8418 system installed by an NSI/SSAIB-certificated company can obtain a police URN, similar to intruder systems. Check the current NPCC policy **(verify)**.
- **Key design and operation points:**
  - Detection zones are matched to camera views, so every activation can be seen.
  - Pre-alarm and post-alarm recording.
  - Regular automated test signals and fault monitoring of the transmission path.
  - Detection is set when the site is closed.
  - Documented response plans, and a false alarm management regime at the RVRC.
- **Maintenance:** preventive visits are typically **twice yearly** for BS 8418 systems **(verify)**. Also clean cameras and detectors, check alignment against reference images, test audio challenge and confirm RVRC alarm receipt.
- **False activation control:** check vegetation growth, spider webs, lighting changes, animals, analytics sensitivity and detector alignment.

## UK GDPR, DPA 2018 and ICO considerations (CCTV)

**The customer (the controller) is responsible for compliance**, but a professional installer should advise and document. Many disputes start with "the installer said it was fine".

- **Lawful basis:** usually legitimate interests (crime prevention, safety). The controller should record a legitimate interests assessment.
- **DPIA (data protection impact assessment):** required where processing is likely to be high risk, such as systematic monitoring of public areas on a large scale, ANPR, facial recognition or monitoring of workers. Good practice for any significant system.
- **Transparency:** clear signs at entry points. They should say CCTV is in operation, who operates it (the controller) and how to contact them, with the purpose where not obvious. A privacy notice should give full details.
- **Proportionality and minimisation:**
  - Avoid cameras in intrusive areas such as toilets or changing areas (only in truly exceptional, justified cases, with safeguards).
  - Mask private areas and neighbours' property using privacy masking.
  - Don't point cameras over the boundary unnecessarily.
- **Audio recording** is highly intrusive and should normally be **off** unless specifically justified and documented.
- **Retention:** set a defined period, justify it and auto-overwrite. Don't keep footage "just in case".
- **Security:**
  - change default passwords, use strong credentials and role-based access
  - apply firmware updates
  - secure remote access (no open port forwarding without controls)
  - log who viewed or exported footage
  - encrypt exports where possible
- **Subject access requests:** individuals can request footage of themselves. Respond within one month (extendable by up to two further months for complex requests). Redact or blur third parties where necessary. The system should be able to search and export efficiently.
- **Disclosure to police:** allowed for crime prevention and detection under DPA 2018 exemptions. Record requests and disclosures.
- **ICO data protection fee:** most organisations processing personal data (including CCTV) must pay it unless exempt **(check the current fee tier)**.
- **Domestic CCTV:** if cameras capture areas beyond the homeowner's boundary (street, neighbours), the homeowner has data protection obligations. Advise on positioning and masking.
- **Installer as processor:** if Salts hosts, monitors or remotely accesses footage (cloud video, remote support, RVRC), there should be a written processor agreement or terms with the customer.
- **Recent change:** the Data (Use and Access) Act 2025 amended parts of UK GDPR and DPA 2018. It affects areas such as complaints handling, SAR searches ("reasonable and proportionate") and legitimate interests. **Check ICO guidance for current detail (verify).**

## Surveillance Camera Code of Practice

- Issued under the Protection of Freedoms Act 2012. It is **mandatory for "relevant authorities"** (police and local authorities) and **encouraged voluntarily** for everyone else, including private operators and commercial sites.
- It is built around **12 guiding principles**: legitimate purpose, privacy impact, transparency, accountability, clear rules and policies, retention, access control, standards, security, review, effective use for law enforcement and data accuracy.
- A voluntary third-party certification scheme exists for demonstrating compliance with the code.
- The oversight commissioner role and the code's status have been subject to legislative review. **Check current arrangements (verify).**

## Cyber security for IP systems

- **Product Security and Telecommunications Infrastructure (PSTI) regime (from April 2024):** consumer connectable products (including consumer IP cameras) must not ship with universal default passwords, must have a vulnerability reporting route and must state the minimum security update period. For installers, this means choosing compliant products for domestic customers.
- Good practice:
  - put CCTV and access control on a separate network (VLAN)
  - disable unused services (UPnP, Telnet)
  - use the manufacturer's secure cloud or VPN, not port-forwarding
  - keep firmware current
  - use unique credentials per site and a password manager
  - remove engineer access when the contract ends
- Check product vendor restrictions: some public sector customers restrict certain camera manufacturers on security grounds **(verify current government guidance)**.
- Clients increasingly ask for **Cyber Essentials** certification from their security contractors. See `legislation/certification-and-competency.md`.

## CCTV maintenance

| Task | Frequency (typical) |
|---|---|
| Preventive maintenance visit | Annually as a minimum. Twice yearly for BS 8418 or high-risk systems **(verify contract/insurer)** |
| Clean domes, housings and lenses | Each visit (more often in dusty or coastal sites) |
| Check field of view, focus and day/night switching against reference images | Each visit |
| Check recording schedule, retention actually achieved (days on disk), time/date sync (NTP) | Each visit |
| Check disk health (SMART or manufacturer status), RAID status, UPS | Each visit |
| Test export: produce a playable exported clip | Each visit |
| Check firmware and security patches, user accounts and passwords | Each visit, or remotely |
| Check IR illuminators, PSUs, PoE budget, cabling, fixings and brackets | Each visit |
| Check signage is still present and accurate | Each visit |

## Access control standards and hardware

| Standard | Covers |
|---|---|
| BS EN 60839-11-1 | Electronic access control: system and component requirements, security grades 1–4 |
| BS EN 60839-11-2 | Application guidelines |
| BS EN 13637 | Electrically controlled exit systems for use on escape routes |
| BS EN 179 | Emergency exit devices (lever handle or push pad), for staff who know the building |
| BS EN 1125 | Panic exit devices (push bar), for the public |
| BS EN 14846 | Electromechanical locks and striking plates |
| BS 7273-4 | Actuation of door release mechanisms by the fire alarm system |
| BS 8300 / Approved Document M | Accessibility, such as reader and exit button heights |

**Access control features to discuss with the client:**
- credentials (cards, fobs, mobile, PIN, biometrics)
- time zones and anti-passback
- door-forced and door-held-open alarms
- audit trail
- integration with intruder alarm (set/unset), CCTV and visitor management
- lockdown functions (schools)

**Data protection:**
- Access logs are personal data. Set retention and control who can see them.
- **Biometrics** (fingerprint, face) are special category data. They need an Article 9 condition, a DPIA and normally a non-biometric alternative for staff who don't want to use them. The ICO has taken enforcement action against employers using biometric attendance systems without adequate justification.

## Fail-safe vs fail-secure

| Term | Behaviour on loss of power | Typical hardware | Use |
|---|---|---|---|
| **Fail-safe** (fail-unlocked) | Door **unlocks** when power is removed | Maglocks (inherently fail-safe), fail-safe electric strikes, fail-safe motor locks | **Doors on escape routes**, and any door where people must be able to get out |
| **Fail-secure** (fail-locked) | Door **stays locked** when power is removed. Egress is usually by a mechanical handle on the secure side | Fail-secure electric strikes, some motor locks | Perimeter or store doors where security on power loss is needed and escape isn't compromised (free mechanical egress retained) |

**Key rule:** anyone on the escape side must be able to open an escape door **immediately, without a key, card or code**. If an electric lock is used, the fire strategy must support it. The lock must release on:
1. **Fire alarm activation.** The interface is designed to BS 7273-4 at the appropriate category.
2. **Power failure** (fail-safe).
3. **Manual emergency release:** a **green "emergency door release" unit** next to the door on the escape side. It should directly break power to the lock (double-pole) rather than relying on controller software.
4. A **request-to-exit** device (push button or PIR) for normal egress. This is a convenience only, not the emergency release.

## Fire alarm interface: BS 7273-4

- BS 7273-4 defines categories of actuation for door release mechanisms, based on how critical the release is to life safety:
  - **Category A** is the most onerous. It is typically needed where failure to release could trap people, such as electrically locked doors on escape routes.
  - **Categories B and C** are progressively less onerous. They are commonly applied to hold-open devices on fire doors and other less critical releases.
  - The fire risk assessment or fire strategy decides the category. **Confirm it with the designer and check the standard's current definitions before quoting (verify).**
- **Principles** (apply generally):
  - The door must release on a fire alarm signal. Failure of the interface or its wiring must also result in release (fail-safe).
  - Power to the lock should be removed directly by the fire alarm interface (relay contacts or supply), not by a software command through the access control system alone.
  - Use fire-resisting cabling for the interface where required.
  - Test the release at every fire alarm service (with the cause and effect matrix) and record it.
  - Coordinate the access control installer and the fire alarm contractor. The interface is the most common place for gaps in responsibility. Agree in writing who owns the relay and wiring.
- **Maglocks on doors that are also fire doors:** the maglock and its fixings must not compromise the door's fire rating. Use certificated fire-door hardware, and follow the door manufacturer's test evidence.
- **Lifts and security:** where the access control system restricts lift use, confirm fire alarm override (lift homing and firefighter control) takes priority.

## Access control maintenance

| Task | Frequency (typical) |
|---|---|
| Preventive visit | Annually. Every 6 months for high-use or critical sites |
| Test each door: reader, lock, door contact, REX, green break-glass release, door closer and alignment | Each visit |
| Test fire alarm release of all interfaced doors (with the fire alarm engineer or during the fire alarm test) | Each visit (and each fire alarm service) |
| Test PSU and batteries (standby duration where the lock must hold on mains failure; fail-safe doors release) | Each visit |
| Check controller firmware, database backups, time sync, user list housekeeping (leavers removed) | Each visit / remotely |
| Check audit log retention and security settings | Each visit |

## Related files

- `standards/intruder-alarms-and-security.md`
- `standards/fire-extinguishers-and-other.md` (fire doors and hold-open devices)
- `operations/maintenance-contracts-and-scheduling.md` (staff monitoring and GDPR)
