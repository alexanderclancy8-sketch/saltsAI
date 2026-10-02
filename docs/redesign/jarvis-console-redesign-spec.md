THIS IS AN EXECUTION INSTRUCTION FOR YOU, NOT A PROMPT FOR YOU TO REVIEW OR REWRITE.

# Jarvis console redesign: build spec

**App:** J.A.R.V.I.S. for Salts Fire and Security (the Azure app at jarvis-salts…azurewebsites.net)
**Design reference:** `jarvis-console-mockup.html` (supplied with this spec). It is a clickable mockup; treat its layout, tokens and behaviour as the target. Its data is a snapshot from 2 Oct 2026 and must not be copied into the app.
**Written:** 2 Oct 2026

## Goal

Make the console minimal. The agent (core, conversation, message box) is the whole page. Every dashboard section is hidden until asked for and opens as a pop-up. Jarvis talks in plain conversational English.

## Rules for this work

1. Inspect the existing frontend and backend before changing anything. Reuse existing endpoints, data loaders and settings storage. This is a re-skin and re-arrangement plus the fixes listed below, not a rewrite.
2. Do not remove any existing feature. Everything on the current dashboard must still be reachable, in a pop-up.
3. Work in phases, in the order below. After each phase, run the full existing test suite plus new tests, and stop for review. Open a pull request per phase. Never merge or deploy yourself.
4. Auto-fix and Self-improvement are ON from the start, and Jarvis merges his own pull requests when they are green (Alex's decisions, 2 Oct). See "Self-merge" below; this replaces the old rule that only Alex merges. He also deploys them to Azure himself, with a health check and automatic rollback. Do not change standing approvals for anything else (emails, FSM writes, payments).
5. Never print secrets, tokens or the staff report key in the UI, logs or pull request text.

## Phase 1: layout and look

**Design tokens** (copy from the `:root` block in the mockup): deep navy background family shared with Salts FSM, one cyan "core" colour for the agent, amber for working or needs-a-look, green for healthy, red for failing. Display face Oxanium, body IBM Plex Sans, data IBM Plex Mono. Dark only.

**Sign-in page:** animated core, brand, "Identify yourself" heading, password field, Sign in button. Same authentication as now.

**Console shell:**

- Top bar: brand, status (Online, Listening, Working, Speaking), demo-data pill, clock, Voice on/off, Speaks up, Connections, Settings. All labelled with text, no icon-only buttons.
- Left rail (a scrolling chip strip on phones): Approvals, Comms, Issues, Health, Ops, Fleet, Finance, Presence, Coming up. Each shows a single count. Counts needing attention are amber, failures red.
- Centre column, max 720px: the core, a one-line hint, the "Needs you" strip (at most three items, chosen by urgency, each opening its pop-up), the conversation, the message box.
- Message box: shortcuts menu (Briefing, Wrap-up, Team review, Business health, Cash flow, Where's everyone?, Stock, Customers, Marketing), text field, microphone, Send. Send becomes Stop while a reply is streaming. Keep the existing attach button and learned-reply suggestion.

**The core:** a canvas animation whose colour and energy follow the agent's state. Clicking it starts or stops listening. Respect `prefers-reduced-motion` with a static frame.

**Pop-ups:** one drawer component sliding in from the right, closed by a Close button, Escape, or clicking outside. Content per section is exactly what the current dashboard shows:

| Pop-up | Contents |
|---|---|
| Approvals | Awaiting your approval; suggestions |
| Comms | Unread messages |
| Issues | Open issues and fixes |
| Health | Routine tests with Run tests now; alerts |
| Ops | Jobs today, on site, late starts, overdue; engineer list |
| Fleet | Vehicle tracking, or a clear "not connected" state |
| Finance | Position figures; customer watch |
| Presence | Followers and reviews |
| Coming up | Dated reminders |
| Demo data | Which sources are still samples and how to connect them |
| Settings | Voice and display options; link to Connections; Sign out |
| Connections | List of all integrations with status; each row opens its own setup form with a back link |

**Bug to fix here:** the current Settings panel briefly renders wider than the window with its close button off-screen. The new drawer must never exceed the viewport width.

**Acceptance:** at 1280px, 800px and 400px wide there is no horizontal scroll, every rail item opens the right pop-up, every Connections row opens its form, and all existing settings still save.

## Phase 2: how Jarvis talks

1. Update the system prompt: plain conversational British English, short sentences, lead with the answer, no markdown or lists in spoken or chat replies, usually two to four sentences, a follow-up question only when it helps.
2. Add a "How Jarvis talks" setting: Natural (uses the owner's first name) or Formal (uses the existing "what Jarvis calls you" value). Default Natural.
3. Stream replies into the chat as they are generated.
4. Show working: a small line above each reply naming what he is checking ("Checking Salts FSM…"), then the source and time underneath the finished reply.
5. Offer the matching pop-up as a button under a reply when there is detail behind it.
6. Offer up to two follow-up questions as buttons under a reply.
7. Question pop-up: when Jarvis asks something answerable by choosing, show a centred pop-up with his question and two to four answers as real `<button>` elements, plus "Type my own answer". Clicking an answer sends it. This replaces the current question pop-up and fixes issue #8 (options show a text cursor and cannot be clicked).

**Acceptance:** tests cover the tone setting, the choices pop-up (click sends the answer, Escape closes without sending), and streaming with Stop.

## Phase 3: fixes found on 2 Oct

1. **Demo data must not be reasoned from.** When a source is still demo (accounts, socials, stock, staff register), Jarvis says he cannot answer from it and names what needs connecting. He must not quote demo names or figures as if real. Keep the visible demo labels.
2. **Scheduled checks stay quiet.** A scheduled check that finds no change posts nothing to the conversation. All runs are recorded in an activity log, shown as one collapsed line in the chat ("Pull request watch · 7 checks, no change") that expands on click.
3. **Speech-to-text failing.** The routine test reports no OpenAI API key. Make the failure visible in the top-bar status and fall back cleanly to browser speech recognition; do not fail silently.
4. **RAM Tracking failing.** The vehicle API returns 404 (issues #9 and #11). Investigate the endpoint and credentials detection; show "not connected" in Fleet until fixed.
5. **Staff report link.** Settings currently prints the full report address including its key. Replace with a "Copy staff report link" button and never render the key as text.

## Phase 4: additions

1. **Approvals inbox.** Everything Jarvis wants to send or change queues in the Approvals pop-up and as a card in the chat, showing exactly what will happen, with Approve, Edit and Don't send. Nothing is sent without a click. Failed actions (for example actions #71 and #72) show their error and a Retry.
2. **Daily rhythm.** Morning briefing and end-of-day wrap-up, each under a minute when spoken, posted to Teams as well as the console. Times set in Schedules.
3. **Phone layout.** One-handed: large core to talk, approvals reachable in one tap, pop-ups full-screen.
4. **Memory pop-up.** Lists what Jarvis has learned about the business ("Things Jarvis should know" plus learned replies), each entry editable and deletable.
5. **Team mode.** A cut-down console for engineers and office staff: no Finance, no Approvals, no Connections. Enforce on the backend, not just by hiding buttons.

## Auto-fix and Self-improvement in the new console

Both are on from day one, so the console must make their work easy to follow:

1. Every pull request Jarvis raises appears as a card in the chat and in the Issues pop-up: what it changes, why, test result, and whether it merged.
2. The Issues pop-up shows, per issue, whether Jarvis is working on a fix, has a pull request open, or needs a human.
3. Health shows the last auto-fix and self-improvement run, and any that failed, with the error.
4. Connections shows both as On, with their settings one tap away.
5. A refused or failed pull request (as with PR #68 on 2 Oct) is reported once, plainly, with what Alex needs to do next.

## Self-merge

Applies to pull requests Jarvis raises through Auto-fix and Self-improvement.

1. **Green: merge.** When every required check passes and there are no conflicts, Jarvis merges the pull request himself.
2. **Not green: fix, then merge.** When a check fails, Jarvis reads the failure, pushes a fix to the same branch, and waits for the checks to run again. When they pass, he merges.
3. **Attempt limit.** After 3 failed fix attempts on one pull request he stops, leaves it open, and tells Alex what is failing. No endless loops.
4. **Green must be earned.** A fix may not delete, skip or weaken a test, lower a coverage or lint threshold, or edit the CI workflow to make a check pass. A pull request that touches tests or CI configuration in those ways is never self-merged; it waits for Alex.
5. **Protected areas, never self-merged:** login and password handling, secrets and connection credentials, approval and standing-approval logic, and this self-merge logic itself. Changes there wait for Alex.
6. **Tell Alex every time.** Each merge posts one short message to Teams and the console: what changed, why, and the pull request link.
7. **Off switch.** Connections shows a "Jarvis merges his own fixes" switch. Turning it off returns to Alex-merges-only immediately.
8. **Deploy.** After a self-merge, Jarvis runs the "Deploy Jarvis to Azure" workflow himself (Alex's decision, 2 Oct).
   - One deploy at a time. If several pull requests merge close together, deploy once with all of them.
   - Before deploying, record the currently live version so it can be restored.
   - After deploying, run a health check: the app answers, sign-in works, the routine tests pass at least as many as before the deploy, and Jarvis can still reply to a test message.
   - If the deploy or the health check fails, roll back to the recorded version automatically, do not retry the deploy, and tell Alex what failed. The rollback must not depend on the new version working, so run it from the GitHub workflow, not from inside the app.
   - Post one message to Teams and the console for each deploy: what went live, or that it was rolled back and why.
   - The off switch in rule 7 also stops self-deploys.
9. **Scheduled check prompt.** Update the existing "Pull request watch" instruction, which currently says never merge or approve, to match these rules.

Tests must cover: green merges; red is fixed then merged; the attempt limit; a weakened test blocks self-merge; a protected-area change blocks self-merge; the off switch; a failed health check triggers rollback and a message to Alex.

## Out of scope

- Any new integration.
- Changes to Salts FSM itself.

## Open questions for Alex

1. Should the light theme exist at all, or is the console dark only?
2. Who counts as "team" for Team mode: engineers only, or office staff too?
3. What time should the morning briefing and the wrap-up run?
