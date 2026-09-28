# Setting up Jarvis on Azure

A step-by-step guide that a person, or Claude Desktop working in Chrome, can follow. Everything happens in
**Azure Cloud Shell**, a terminal inside the Azure portal, so there's nothing to install on a PC. It takes
about 20 minutes.

**What it builds.** Jarvis runs as its own web app on its own small Linux App Service plan (B1, about £10 a
month), in its own resource group. It can't share the Salts FSM plan because that plan is Windows, and Jarvis
needs Linux. Salts FSM isn't touched at all. Only the people you name can open Jarvis, using their Microsoft 365
sign-in. Jarvis runs in Azure, so it works whether or not anyone's laptop is on.

## What's being created (read this first)

- **Jarvis is a brand-new, separate web app.** It doesn't exist yet, and it is **not** Salts FSM. Nothing is
  being added to, uploaded to or changed in the Salts FSM web app.
- **The setup script creates everything itself**, in one go:

  | What | Name |
  |---|---|
  | New resource group | `rg-jarvis` |
  | New Linux App Service plan (B1) | `salts-jarvis-plan` |
  | New web app | `salts-jarvis` |
  | New storage account | `saltsjarvis...` |
  | Microsoft sign-in registration | "Jarvis (salts-jarvis)" |

  **Don't create a web app, plan or anything else by hand in the portal.** Just run the commands below and
  answer the questions.
- It doesn't use or change anything that already exists. Salts FSM, its Windows plan and its resource group
  are all left exactly as they are.
- When it's finished, there's one new resource group, `rg-jarvis`, holding everything Jarvis needs.

## Rules for Claude (if Claude is doing this)

- **Never type passwords, tokens or keys yourself.** When a prompt asks for one, stop and ask the owner to
  type or paste it.
- Only run the commands in this guide. Don't click around the portal changing things. In particular, never
  change, restart or delete the Salts FSM web app, its plan or its settings.
- If a command prints `Stopped:` or an error, stop and show the owner the message. Don't try workarounds.

## 1. Open Cloud Shell

1. Go to https://portal.azure.com (already signed in).
2. Click the `>_` icon in the top bar. If it asks, choose **Bash**. If it asks about storage, choose
   **No storage account required**, then pick the subscription Salts FSM is in.
3. Wait for the `$` prompt.

## 2. Get the Claude token (so Jarvis uses the Claude Max plan)

Paste this into Cloud Shell:

```bash
curl -fsSL https://claude.ai/install.sh | bash
~/.local/bin/claude setup-token
```

It shows a link. Open it, and the **owner** signs in with the Claude Max account and approves. If Cloud Shell
then asks for a code, the owner copies it from the browser and pastes it in. It prints a long token starting
`sk-ant-oat01-`. The owner keeps it handy for step 3 (it's a secret, so don't save it anywhere shared).

## 3. Create and set up the Jarvis web app

This step creates the new `salts-jarvis` web app. Paste this into Cloud Shell:

```bash
git clone -b claude/jarvis-company-ai-assistant-gaj3mj https://github.com/alexanderclancy8-sketch/saltsAI.git
cd saltsAI
LOCATION=ukwest PLAN= bash infra/deploy.sh
```

(`PLAN=` with nothing after it means "make a new Linux plan for Jarvis". The Salts FSM plan is Windows, so
Jarvis can't share it. `LOCATION=ukwest` puts Jarvis in the UK West region, because this subscription's
allowance of B1 plans in UK South is already used by the Salts FSM test app.)

It asks these questions in order. Answer one at a time, and only once each question is showing. Anything
typed or pasted before a question appears is ignored. If an answer looks wrong, it tells you and asks again.

| Question | Answer |
|---|---|
| **Password for the Jarvis display** (twice) | The owner types a new password. It's a backup way in. |
| **Claude token** | The owner pastes the `sk-ant-oat01-...` token from step 2. |
| **Microsoft 365 addresses allowed to sign in** | The owner's and the business partner's work email addresses, separated by a space. |
| **Carry on? (y/n)** | It lists what it's about to create. Check it says a *new* resource group, plan and web app, then type `y`. Nothing is created before this. |

Then it builds everything, which takes about 10 minutes. The upload step alone can sit quietly for around
5 minutes. At the end it prints:

- **Jarvis:** the web address to bookmark.
- **Staff report link:** the page staff use to report problems. Save it and share it with staff.
- `Microsoft sign-in is on. Only these people can open Jarvis: ...`

If it stops saying the name is taken, run it again with a different name:
`APP_NAME=salts-jarvis-hq LOCATION=ukwest PLAN= bash infra/deploy.sh`

If it says an email address wasn't found, it shows which account and directory Azure is using. Copy that
for the owner, leave the question blank, and carry on. Jarvis works with the password, and Microsoft sign-in
can be switched on later.

### If you already made the web app by hand in the portal

Don't run step 3. Turn the web app you made into Jarvis instead, using its name (the first part of its
address, e.g. `jarvis-salts`):

```bash
cd ~/saltsAI
git pull
bash infra/deploy.sh adopt jarvis-salts
```

If there's more than one web app with that name, it lists them. Run it again with the resource group of the
right one on the end, e.g. `bash infra/deploy.sh adopt jarvis-salts rg-jarvis`. It checks the app is Linux on a
paid plan (B1 or bigger). Then it asks for the password and Claude token,
and asks you to type the app's name to confirm. After that it sets the app up and uploads Jarvis. For
later commands, put `APP_NAME=jarvis-salts` in front, e.g. `APP_NAME=jarvis-salts bash infra/deploy.sh update`.

## 4. Check it works

Open the Jarvis address in a new tab. You should get the Microsoft sign-in page, then the Jarvis display. The
first start takes a few minutes while Azure installs everything, so if you see an error page, wait 5 minutes
and refresh. If it still isn't up after 10 minutes, run this and show the owner the last lines:

```bash
az webapp log tail -g "$(az webapp list --query "[?name=='salts-jarvis'].resourceGroup | [0]" -o tsv)" -n salts-jarvis
```

Press `Ctrl+C` to stop the log.

## 5. Add the other connections (the owner can do this any time)

```bash
cp .env.example jarvis.env
code jarvis.env
```

This opens an editor. The **owner** fills in whatever keys they have: Microsoft 365, Sage, ElevenLabs,
Deepgram and so on (`README.md` explains each one). Also fill in `OWNER_EMAIL`, and `PARTNER_NAME` and
`PARTNER_EMAIL`, so Jarvis knows who's talking. Blank lines are skipped. Save with `Ctrl+S`, then:

```bash
bash infra/deploy.sh settings jarvis.env
rm jarvis.env
```

## Later

- **New version of Jarvis:** `cd saltsAI && git pull && bash infra/deploy.sh update`. If Cloud Shell has
  been reset and the folder's gone, repeat the `git clone` line from step 3 first.
- **Change who can sign in:** `bash infra/deploy.sh signin you@... partner@... officemanager@...`. This sets
  the full list, so anyone left off loses access.
- **Every 2 years:** the sign-in secret expires. Run the same `signin` command again to renew it.
