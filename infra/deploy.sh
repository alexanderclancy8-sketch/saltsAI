#!/usr/bin/env bash
# Put Jarvis on Azure from Azure Cloud Shell (portal.azure.com, the >_ icon, Bash).
#
#   bash infra/deploy.sh                      first time: sets up Jarvis (sharing an existing plan if you pick one),
#                                             uploads the code and, if you name people, turns on Microsoft sign-in
#   bash infra/deploy.sh update               upload the latest Jarvis code (settings are kept)
#   bash infra/deploy.sh settings FILE        copy the filled-in values from an env file (like .env.example) into the app
#   bash infra/deploy.sh adopt NAME [GROUP]   turn a Linux web app you made by hand in the portal into Jarvis
#   bash infra/deploy.sh token                replace the Claude token (or API key) Jarvis uses
#   bash infra/deploy.sh voice                give Jarvis Azure's natural British voice (free tier where available)
#   bash infra/deploy.sh voicetest            fetch a sample of Jarvis's Azure voice to listen to
#   bash infra/deploy.sh m365                 register Jarvis for Microsoft 365 (mail, calendar, Teams) and save it
#   bash infra/deploy.sh secret NAME          save one key or password (e.g. ELEVENLABS_API_KEY) without showing it
#   bash infra/deploy.sh signin EMAIL...      only these Microsoft 365 accounts can open Jarvis (run again to change
#                                             the list; each run also issues a fresh sign-in secret, valid 2 years)
#
# Change the defaults with e.g.  APP_NAME=salts-jarvis-2 PLAN=my-plan bash infra/deploy.sh
#
# Everything the script creates is tagged app=jarvis. It stops if the web app name is already taken, and it only
# ever uploads code or changes settings on a web app tagged app=jarvis, so it cannot overwrite Salts FSM or any
# other app - including other apps on a plan Jarvis shares.
set -euo pipefail

APP_NAME="${APP_NAME:-salts-jarvis}"
RG="${RG:-rg-jarvis}"
LOCATION="${LOCATION:-uksouth}"
TAG="jarvis"
SECRET_SETTING="MICROSOFT_PROVIDER_AUTHENTICATION_SECRET"
cd "$(dirname "$0")/.."

die() { echo "Stopped: $*" >&2; exit 1; }

command -v az >/dev/null || die "the Azure CLI isn't here. Run this in Azure Cloud Shell (portal.azure.com, the >_ icon)."

# Finds the Jarvis web app wherever it lives and points RG at its resource group.
find_jarvis_app() {
  # Several apps can share a name (in different resource groups), so look for the one tagged as Jarvis.
  RG="$(az webapp list --query "[?name=='$APP_NAME' && tags.app=='$TAG'].resourceGroup | [0]" -o tsv)"
  [ -z "$RG" ] || return 0
  [ -z "$(az webapp list --query "[?name=='$APP_NAME'].name | [0]" -o tsv)" ] ||
    die "$APP_NAME isn't Jarvis (it has no app=jarvis tag), so it's been left alone."
  die "there's no web app called $APP_NAME. Check APP_NAME, or run the first-time setup."
}

# True if Jarvis is answering at https://HOST.
jarvis_up() { curl -s --max-time 10 "https://$1/healthz" 2>/dev/null | grep -q '"ok"'; }

deploy_code() {
  find_jarvis_app
  local tmp attempt host was_up=0 signin_hiccup="token from portal|credential problem"
  tmp="$(mktemp -d)"
  host="$(az webapp show -g "$RG" -n "$APP_NAME" --query defaultHostName -o tsv)"
  ! jarvis_up "$host" || was_up=1
  zip -qr "$tmp/jarvis.zip" jarvis knowledge requirements.txt ./*.yaml -x "*/__pycache__/*"
  echo "Uploading Jarvis to $APP_NAME..."
  for attempt in 1 2 3; do
    # --async: hand the zip over and don't hold the connection open while Azure installs the packages. That
    # takes longer than Azure's front door will wait, which otherwise shows up as a 502 error.
    if az webapp deploy -g "$RG" -n "$APP_NAME" --src-path "$tmp/jarvis.zip" --type zip --async true -o none \
      2>&1 | tee "$tmp/log"; then
      rm -rf "$tmp"
      wait_until_up "$host" "$was_up"
      return 0
    fi
    # Cloud Shell's sign-in sometimes times out before the upload starts; that's worth another go.
    grep -qiE "$signin_hiccup" "$tmp/log" && [ "$attempt" -lt 3 ] || break
    echo "Cloud Shell's sign-in timed out, so nothing was uploaded. Trying again (attempt $((attempt + 1)) of 3)..."
    sleep "${RETRY_WAIT:-20}"
  done
  echo
  if grep -qiE "$signin_hiccup" "$tmp/log"; then
    echo "The upload couldn't get a sign-in from Cloud Shell. Jarvis's settings are saved; only the code is missing."
    echo "Run  az login  and follow the code it shows, then upload again with:"
  else
    echo "The upload didn't finish, or Azure is still starting Jarvis. See what's happening with:"
    echo "    az webapp log tail -g $RG -n $APP_NAME"
    echo "To upload again:"
  fi
  echo "    APP_NAME=$APP_NAME bash infra/deploy.sh update"
  rm -rf "$tmp"
}

# After an upload: a running Jarvis restarts by itself; a first install is watched until it answers.
wait_until_up() {
  local host="$1" was_up="$2" i
  if [ "$was_up" = 1 ]; then
    echo "Uploaded. Azure is installing it now, and Jarvis restarts with the new version in about 5 minutes."
    return 0
  fi
  echo "Uploaded. Azure is installing Jarvis's packages now. Checking every 20 seconds until it answers (up to 15 minutes)..."
  for i in $(seq 1 "${WAIT_CHECKS:-45}"); do
    sleep "${WAIT_SECONDS:-20}"
    if jarvis_up "$host"; then
      echo "Jarvis is up: https://$host"
      return 0
    fi
    printf '.'
  done
  echo
  echo "It isn't answering yet. The install may still be going, or it may have failed. Two ways to see:"
  echo "    az webapp log deployment show -n $APP_NAME -g $RG --query \"[].message\" -o tsv | tail -25"
  echo "    az webapp log tail -n $APP_NAME -g $RG      (what Jarvis prints as it starts; Ctrl+C to stop)"
}

# Gives Jarvis a natural British voice from Azure Speech, created and connected here so there's no key to copy.
setup_voice() {
  find_jarvis_app
  local name="$APP_NAME-voice" region="${SPEECH_REGION:-uksouth}" voice="${VOICE:-en-GB-RyanNeural}" key
  if [ -z "$(az cognitiveservices account list -g "$RG" --query "[?name=='$name'].name | [0]" -o tsv)" ]; then
    echo "Setting up Azure's speech service for Jarvis's voice (a minute or two)..."
    az provider register --namespace Microsoft.CognitiveServices --wait -o none
    if az cognitiveservices account create -n "$name" -g "$RG" -l "$region" --kind SpeechServices --sku F0 \
      --tags app="$TAG" --yes -o none 2>/dev/null; then
      echo "Using the free tier: 500,000 characters of speech a month, far more than Jarvis needs."
    else
      echo "Azure allows one free speech service per subscription and it's taken, so this one is pay-as-you-go:"
      echo "about £12 per million characters, which is pennies a day for Jarvis."
      az cognitiveservices account create -n "$name" -g "$RG" -l "$region" --kind SpeechServices --sku S0 \
        --tags app="$TAG" --yes -o none
    fi
  fi
  key="$(az cognitiveservices account keys list -n "$name" -g "$RG" --query key1 -o tsv)"
  az webapp config appsettings set -g "$RG" -n "$APP_NAME" -o none \
    --settings "AZURE_SPEECH_KEY=$key" "AZURE_SPEECH_REGION=$region" "AZURE_TTS_VOICE=$voice"
  echo "Done. Jarvis now speaks with Azure's $voice British voice. It restarts to pick this up, so refresh the"
  echo "display in a minute. To try another voice, e.g. Thomas:  VOICE=en-GB-ThomasNeural bash infra/deploy.sh voice"
  echo "(others: en-GB-OliverNeural, en-GB-AlfieNeural, en-GB-ElliotNeural, en-GB-EthanNeural, en-GB-NoahNeural)."
  echo "If an ElevenLabs key is added later, Jarvis uses ElevenLabs instead."
}

# Asks Azure for a sample of Jarvis's voice using Jarvis's own settings, and saves it to listen to.
voice_test() {
  find_jarvis_app
  local key region voice style out="$HOME/jarvis-voice-test.mp3" code express_open="" express_close=""
  setting() { az webapp config appsettings list -g "$RG" -n "$APP_NAME" --query "[?name=='$1'].value | [0]" -o tsv; }
  key="$(setting AZURE_SPEECH_KEY)"
  [ -n "$key" ] || die "Jarvis has no Azure voice yet. Run:  APP_NAME=$APP_NAME bash infra/deploy.sh voice"
  region="$(setting AZURE_SPEECH_REGION)"
  voice="$(setting AZURE_TTS_VOICE)"
  style="$(setting AZURE_TTS_STYLE)"
  style="${style:-chat}"
  if [ "$style" != "none" ]; then
    express_open="<mstts:express-as style='$style'>"
    express_close="</mstts:express-as>"
  fi
  code="$(curl -s -o "$out" -w '%{http_code}' -X POST "https://${region:-uksouth}.tts.speech.microsoft.com/cognitiveservices/v1" \
    -H "Ocp-Apim-Subscription-Key: $key" -H "Content-Type: application/ssml+xml" -H "User-Agent: salts-jarvis" \
    -H "X-Microsoft-OutputFormat: audio-24khz-96kbitrate-mono-mp3" \
    --data "<speak version='1.0' xml:lang='en-GB' xmlns='http://www.w3.org/2001/10/synthesis' xmlns:mstts='https://www.w3.org/2001/mstts'><voice name='${voice:-en-GB-RyanNeural}'>${express_open}Good afternoon. Three jobs finished today, and the Kestrel call-out is booked for nine tomorrow.${express_close}</voice></speak>")"
  if [ "$code" = "200" ]; then
    echo "Azure's voice is working (${voice:-en-GB-RyanNeural}, style $style). To hear exactly what Jarvis sounds like:"
    echo "in Cloud Shell's toolbar click 'Manage files' > 'Download', type  jarvis-voice-test.mp3  and play the file."
  else
    echo "Azure refused (HTTP $code): $(head -c 300 "$out")"
    echo "That's why the display falls back to the browser voice. Send this message to whoever set Jarvis up."
  fi
}

# Saves one setting without showing it or leaving it in the shell history, e.g.  secret ELEVENLABS_API_KEY
set_secret() {
  local name="${1:-}" value
  [[ "$name" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "give the setting's name, e.g.  bash infra/deploy.sh secret ELEVENLABS_API_KEY"
  find_jarvis_app
  ask value "Paste the value for $name (it won't show): " token
  [ -n "$value" ] || die "nothing was changed."
  az webapp config appsettings set -g "$RG" -n "$APP_NAME" -o none --settings "$name=$value"
  echo "Saved $name. Jarvis restarts to pick it up."
}

# Replaces the Claude token (or API key) Jarvis uses, without it appearing on screen or in the shell history.
set_token() {
  find_jarvis_app
  local claude_token anthropic_key
  ask_claude
  if [ -n "$claude_token" ]; then
    az webapp config appsettings set -g "$RG" -n "$APP_NAME" -o none --settings "CLAUDE_CODE_OAUTH_TOKEN=$claude_token"
    # An API key alongside the token would make Claude Code bill the API instead of the subscription.
    az webapp config appsettings delete -g "$RG" -n "$APP_NAME" -o none --setting-names ANTHROPIC_API_KEY
  else
    az webapp config appsettings set -g "$RG" -n "$APP_NAME" -o none --settings "ANTHROPIC_API_KEY=$anthropic_key"
    az webapp config appsettings delete -g "$RG" -n "$APP_NAME" -o none --setting-names CLAUDE_CODE_OAUTH_TOKEN
  fi
  echo "Saved. Jarvis restarts to pick it up."
}

# Why an address can't be used for Microsoft sign-in, or nothing if it can.
lookup_problem() {
  local out domains account
  if [[ "$1" != ?*@?*.?* ]]; then
    echo "'$1' isn't an email address."
    return
  fi
  if out="$(az ad user show --id "$1" --query id -o tsv 2>&1)" && [ -n "$out" ]; then return; fi
  domains="$(az rest --method get --url https://graph.microsoft.com/v1.0/organization \
    --query "value[0].verifiedDomains[].name" -o tsv 2>/dev/null | paste -sd ' ' - || true)"
  account="$(az account show --query user.name -o tsv 2>/dev/null || true)"
  echo "$1 wasn't found in the directory your Azure subscription belongs to.
  Azure said:                $(tail -n1 <<<"$out" | sed 's/^ERROR: //')
  You're signed in to Azure as: ${account:-unknown}
  That directory's domains:  ${domains:-unknown}
If ${1#*@} isn't one of those domains, your Azure subscription is in a different directory from your Microsoft
365, so Microsoft sign-in can't use your 365 accounts yet. Leave this blank to use the Jarvis password for now."
}

# Entra object id for a sign-in address, or stop with an explanation.
user_id() {
  local problem
  problem="$(lookup_problem "$1")"
  [ -z "$problem" ] || die "$problem"
  az ad user show --id "$1" --query id -o tsv
}

# Makes the assigned users of the sign-in app exactly the given object ids.
sync_assignments() {
  local sp_id="$1" aid pid
  shift
  local graph="https://graph.microsoft.com/v1.0/servicePrincipals/$sp_id/appRoleAssignedTo"
  local wanted=" $* "
  while IFS=$'\t' read -r aid pid; do
    [ -n "$aid" ] || continue
    if [[ "$wanted" == *" $pid "* ]]; then
      wanted="${wanted/ $pid / }"
    else
      az rest --method delete --url "$graph/$aid" -o none
    fi
  done < <(az rest --method get --url "$graph" --query "value[].[id, principalId]" -o tsv)
  for pid in $wanted; do
    az rest --method post --url "$graph" -o none \
      --body "{\"principalId\": \"$pid\", \"resourceId\": \"$sp_id\", \"appRoleId\": \"00000000-0000-0000-0000-000000000000\"}"
  done
}

enable_signin() {
  [ "$#" -gt 0 ] || die "name everyone allowed in, e.g.  bash infra/deploy.sh signin you@company.co.uk partner@company.co.uk"
  find_jarvis_app
  local email ids=() host tenant authority display app_id sp_id secret site_id body managers
  for email in "$@"; do ids+=("$(user_id "$email")"); done
  host="$(az webapp show -g "$RG" -n "$APP_NAME" --query defaultHostName -o tsv)"
  site_id="$(az webapp show -g "$RG" -n "$APP_NAME" --query id -o tsv)"
  tenant="$(az account show --query tenantId -o tsv)"
  authority="$(az cloud show --query endpoints.activeDirectory -o tsv)"
  display="Jarvis ($APP_NAME)"

  app_id="$(az ad app list --display-name "$display" --query "[0].appId" -o tsv)"
  if [ -z "$app_id" ]; then
    echo "Registering Jarvis for Microsoft sign-in..."
    app_id="$(az ad app create --display-name "$display" --sign-in-audience AzureADMyOrg \
      --web-redirect-uris "https://$host/.auth/login/aad/callback" --enable-id-token-issuance true --query appId -o tsv)"
  fi
  sp_id="$(az ad sp show --id "$app_id" --query id -o tsv 2>/dev/null || true)"
  [ -n "$sp_id" ] || sp_id="$(az ad sp create --id "$app_id" --query id -o tsv)"
  # Nobody but the people assigned below can sign in at all.
  az ad sp update --id "$sp_id" --set appRoleAssignmentRequired=true -o none
  sync_assignments "$sp_id" "${ids[@]}"

  secret="$(az ad app credential reset --id "$app_id" --display-name jarvis-signin --years 2 --query password -o tsv)"
  managers="$(IFS=,; echo "$*" | tr '[:upper:]' '[:lower:]')"
  az webapp config appsettings set -g "$RG" -n "$APP_NAME" -o none \
    --settings "$SECRET_SETTING=$secret" "MANAGER_EMAILS=$managers"

  # App Service's built-in sign-in in front of the whole app, except the health check and the staff report page.
  body="$(mktemp)"
  cat >"$body" <<JSON
{
  "properties": {
    "platform": {"enabled": true},
    "globalValidation": {
      "requireAuthentication": true,
      "unauthenticatedClientAction": "RedirectToLoginPage",
      "redirectToProvider": "azureactivedirectory",
      "excludedPaths": ["/healthz", "/report", "/api/issues/report", "/static/hud.css", "/static/icon.svg"]
    },
    "identityProviders": {
      "azureActiveDirectory": {
        "enabled": true,
        "registration": {
          "clientId": "$app_id",
          "clientSecretSettingName": "$SECRET_SETTING",
          "openIdIssuer": "${authority%/}/$tenant/v2.0"
        }
      }
    },
    "login": {"tokenStore": {"enabled": false}},
    "httpSettings": {"requireHttps": true}
  }
}
JSON
  az rest --method put --url "https://management.azure.com$site_id/config/authsettingsV2?api-version=2023-12-01" \
    --body "@$body" -o none
  rm -f "$body"
  echo "Microsoft sign-in is on. Only these people can open Jarvis: $*"
}

# Text pasted into Cloud Shell can arrive before a question is asked. Throw it away, so a stray line (such as
# the next command in a guide) is never taken as the answer.
drain_input() {
  local junk
  [ -t 0 ] || return 0
  while read -r -t 0.2 junk; do :; done
  return 0
}

# ask VAR "question" [secret|token] - asks once, trims spaces, stores the answer in VAR. Secrets aren't shown.
ask() {
  local __ans
  drain_input
  if [ -n "${3:-}" ]; then
    read -rsp "$2" __ans
    echo
    # A long token copied from a wrapped line can arrive in pieces; join them up.
    local __more=""
    if [ "$3" = "token" ] && [ -t 0 ]; then
      while IFS= read -r -t 0.3 __more; do __ans+="$__more"; done
      __ans+="$__more"
    fi
  else
    read -rp "$2" __ans
  fi
  __ans="$(sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' <<<"$__ans")"
  printf -v "$1" '%s' "$__ans"
}

# Sets the caller's owner_password.
ask_password() {
  local confirm
  while :; do
    ask owner_password "Choose a password for the Jarvis display (you won't see it as you type): " secret
    ask confirm "Type it again: " secret
    if [ -z "$owner_password" ]; then echo "The password can't be blank."
    elif [ "$owner_password" != "$confirm" ]; then echo "Those didn't match. Try again."
    else break; fi
  done
}

# Sets the caller's claude_token, or anthropic_key if the token is left blank.
ask_claude() {
  anthropic_key=""
  while :; do
    ask claude_token "Paste the Claude token from 'claude setup-token' (blank to use an API key instead): " token
    claude_token="${claude_token//[[:space:]]/}"
    [ -z "$claude_token" ] || [[ "$claude_token" =~ ^sk-ant-oat[A-Za-z0-9_-]+$ ]] && break
    echo "That doesn't look like a Claude token (it starts sk-ant-oat01-). Paste it again."
  done
  while [ -z "$claude_token" ]; do
    ask anthropic_key "Paste the Anthropic API key: " token
    anthropic_key="${anthropic_key//[[:space:]]/}"
    [[ "$anthropic_key" =~ ^sk-ant-api[A-Za-z0-9_-]+$ ]] && break
    echo "That doesn't look like an API key (it starts sk-ant-api). Paste it again."
  done
}

# Registers Jarvis for Microsoft 365 (mail, calendar, Teams meeting transcripts) and saves the settings.
m365() {
  find_jarvis_app
  local GRAPH_APP_ID=00000003-0000-0000-c000-000000000000
  local permissions=(Mail.ReadWrite Mail.Send Calendars.Read Reports.Read.All OnlineMeetingTranscript.Read.All)
  local display="Jarvis M365 ($APP_NAME)" app_id sp_id tenant mailbox secret role_id perm consented=0

  ask mailbox "Your Microsoft 365 mailbox (the one Jarvis reads and sends from): "
  [[ "$mailbox" == ?*@?*.?* ]] || die "that doesn't look like an email address."
  tenant="$(az account show --query tenantId -o tsv)"

  app_id="$(az ad app list --display-name "$display" --query "[0].appId" -o tsv)"
  if [ -z "$app_id" ]; then
    echo "Registering Jarvis for Microsoft 365..."
    app_id="$(az ad app create --display-name "$display" --sign-in-audience AzureADMyOrg --query appId -o tsv)"
  fi
  sp_id="$(az ad sp show --id "$app_id" --query id -o tsv 2>/dev/null || true)"
  [ -n "$sp_id" ] || sp_id="$(az ad sp create --id "$app_id" --query id -o tsv)"

  echo "Requesting the Microsoft Graph permissions Jarvis needs..."
  for perm in "${permissions[@]}"; do
    # Looked up by name rather than a hardcoded id, since these can change and vary between clouds.
    role_id="$(az ad sp show --id "$GRAPH_APP_ID" --query "appRoles[?value=='$perm'].id | [0]" -o tsv)"
    [ -n "$role_id" ] || die "couldn't find the Microsoft Graph permission '$perm' - Microsoft may have renamed it.
Add it by hand in Entra ID > App registrations > $display > API permissions, then run this again."
    az ad app permission add --id "$app_id" --api "$GRAPH_APP_ID" --api-permissions "$role_id=Role" -o none
  done

  echo "Requesting admin consent (this only works if you're a Global Administrator)..."
  sleep 5  # a freshly-added permission can take a few seconds to be visible to consent against
  az ad app permission admin-consent --id "$app_id" 2>/dev/null && consented=1

  secret="$(az ad app credential reset --id "$app_id" --display-name jarvis-m365 --years 2 --query password -o tsv)"
  az webapp config appsettings set -g "$RG" -n "$APP_NAME" -o none \
    --settings "MS_TENANT_ID=$tenant" "MS_CLIENT_ID=$app_id" "MS_CLIENT_SECRET=$secret" "MS_MAILBOX=$mailbox"

  echo
  echo "Saved. Jarvis restarts to pick this up."
  if [ "$consented" = 1 ]; then
    echo "Admin consent was granted."
  else
    echo "You'll need a Global Administrator to approve access, by opening this link and accepting:"
    echo "    https://login.microsoftonline.com/$tenant/adminconsent?client_id=$app_id"
    echo "Until that's done, Jarvis's Microsoft 365 features won't work."
  fi
  echo
  echo "Recommended: without a further step, this lets Jarvis read and send mail for ANY mailbox in your"
  echo "organisation, not just $mailbox. To restrict it, in Exchange Online PowerShell (Cloud Shell's PowerShell"
  echo "tab, or portal.exchange.microsoft.com > ... > Connect-ExchangeOnline) run:"
  echo "    New-ApplicationAccessPolicy -AppId $app_id -PolicyScopeGroupId $mailbox -AccessRight RestrictAccess -Description Jarvis"
}

# Turns a Linux web app made by hand in the portal into Jarvis.
adopt() {
  local groups kind site_linux plan_linux plan_kind plan_name plan_id host tier others site_id go
  local owner_password claude_token anthropic_key
  [ -n "${1:-}" ] || die "give the name of the web app you made, e.g.  bash infra/deploy.sh adopt jarvis-salts"
  APP_NAME="$1"
  [[ "${APP_NAME,,}" != *fsm* ]] || die "$APP_NAME looks like a Salts FSM app, so it's been left alone."
  if [ -n "${2:-}" ]; then
    RG="$2"
  else
    groups="$(az webapp list --query "[?name=='$APP_NAME'].resourceGroup" -o tsv)"
    [ -n "$groups" ] || die "there's no web app called $APP_NAME in this subscription."
    if [ "$(wc -l <<<"$groups")" -gt 1 ]; then
      echo "There's more than one web app called $APP_NAME:"
      az webapp list -o table \
        --query "[?name=='$APP_NAME'].{ResourceGroup:resourceGroup, Kind:kind, Address:defaultHostName}"
      die "run it again naming the resource group of the one to use, e.g.  bash infra/deploy.sh adopt $APP_NAME rg-jarvis"
    fi
    RG="$groups"
  fi
  # One value per query: an empty field in a multi-value tsv row would shift the rest along.
  site_id="$(az webapp show -g "$RG" -n "$APP_NAME" --query id -o tsv)"
  [ -n "$site_id" ] || die "there's no web app called $APP_NAME in resource group $RG."
  plan_id="$(az webapp show -g "$RG" -n "$APP_NAME" --query "appServicePlanId || serverFarmId" -o tsv)"
  host="$(az webapp show -g "$RG" -n "$APP_NAME" --query defaultHostName -o tsv)"
  kind="$(az webapp show -g "$RG" -n "$APP_NAME" --query kind -o tsv)"
  site_linux="$(az webapp show -g "$RG" -n "$APP_NAME" --query reserved -o tsv)"
  plan_name="$(az appservice plan show --ids "$plan_id" --query name -o tsv)"
  plan_kind="$(az appservice plan show --ids "$plan_id" --query kind -o tsv)"
  plan_linux="$(az appservice plan show --ids "$plan_id" --query reserved -o tsv)"
  tier="$(az appservice plan show --ids "$plan_id" --query sku.tier -o tsv)"
  if [ "${plan_linux,,}" != "true" ] && [ "${site_linux,,}" != "true" ] && [[ "$kind$plan_kind" != *linux* ]]; then
    die "$APP_NAME (resource group $RG) looks like Windows to Azure, and Jarvis needs Linux.
  App kind: '$kind', app Linux flag: '$site_linux', plan: '$plan_name', plan kind: '$plan_kind', plan Linux flag: '$plan_linux'
If the portal says this app is Linux, send these details to whoever set Jarvis up. Otherwise, create a new web app
with Publish: Code, Runtime stack: Python 3.12 and Operating System: Linux, then adopt that one."
  fi
  [[ "$tier" != "Free" && "$tier" != "Shared" ]] || die "$APP_NAME is on a $tier plan, which switches apps off when
nobody's using them, so Jarvis's briefings, checks and reminders wouldn't run. Move it to a Basic (B1) plan or
bigger: in the portal open the web app, then 'Scale up (App Service plan)'."
  others="$(az webapp list --query "[?appServicePlanId=='$plan_id' && name!='$APP_NAME'].name" -o tsv | paste -sd ' ' -)"

  echo "This turns the web app $APP_NAME (resource group $RG, plan $plan_name, $tier) into Jarvis:"
  echo "  - sets it to Python 3.12 with Jarvis's startup command, WebSockets, Always On and a health check"
  echo "  - adds Jarvis's settings and uploads the Jarvis code, replacing anything already on it"
  [ -z "$others" ] || echo "  - its plan also runs: $others. Those apps aren't changed, but they'll share memory and processor."
  ask_password
  ask_claude
  ask go "To confirm, type the web app's name ($APP_NAME): "
  [ "$go" = "$APP_NAME" ] || die "nothing was changed."

  local staff_key settings
  staff_key="$(openssl rand -hex 12)"
  az resource tag --ids "$site_id" --tags app="$TAG" --is-incremental -o none
  az webapp update -g "$RG" -n "$APP_NAME" --https-only true -o none
  az webapp config set -g "$RG" -n "$APP_NAME" -o none --linux-fx-version "PYTHON|3.12" \
    --startup-file "python -m jarvis" --web-sockets-enabled true --always-on true --ftps-state Disabled \
    --min-tls-version 1.2 --generic-configurations '{"healthCheckPath": "/healthz"}'
  settings=(SCM_DO_BUILD_DURING_DEPLOYMENT=true WEBSITES_ENABLE_APP_SERVICE_STORAGE=true WEBSITES_PORT=8000
    DATA_DIR=/home/data "FORWARDED_ALLOW_IPS=*" "PUBLIC_BASE_URL=https://$host"
    "JARVIS_OWNER_PASSWORD=$owner_password" "JARVIS_SECRET_KEY=$(openssl rand -hex 32)" "STAFF_REPORT_KEY=$staff_key")
  if [ -n "$claude_token" ]; then settings+=("CLAUDE_CODE_OAUTH_TOKEN=$claude_token"); else settings+=("ANTHROPIC_API_KEY=$anthropic_key"); fi
  az webapp config appsettings set -g "$RG" -n "$APP_NAME" --settings "${settings[@]}" -o none
  deploy_code

  echo
  echo "Jarvis:              https://$host  (password: the one you just chose)"
  echo "Staff report link:   https://$host/report?key=$staff_key"
  echo "Add your other keys: fill in a copy of .env.example, then  APP_NAME=$APP_NAME bash infra/deploy.sh settings <that file>"
}

setup() {
  local existing plan_info plan_id="" plan_rg plan_location plan_linux plan_tier others
  local owner_password confirm claude_token anthropic_key staff_key managers url email
  existing="$(az webapp list --query "[?name=='$APP_NAME'] | [0].[resourceGroup, tags.app]" -o tsv)"
  if [ -n "$existing" ]; then
    [[ "$existing" == *$'\t'"$TAG" ]] &&
      die "Jarvis is already set up as $APP_NAME. To upload new code use:  bash infra/deploy.sh update"
    die "a web app called $APP_NAME already exists and it isn't Jarvis. Pick another name, e.g.  APP_NAME=salts-jarvis-2 bash infra/deploy.sh"
  fi

  if [ -z "${PLAN+x}" ]; then
    echo "Your App Service plans. Jarvis can share one - Azure charges per plan, not per app:"
    az appservice plan list -o table \
      --query "[].{Plan:name, ResourceGroup:resourceGroup, Size:sku.name, Linux:reserved, Region:location, Apps:numberOfSites}"
    ask PLAN "Plan to share (blank = a new B1 plan just for Jarvis, about £10 a month): "
  fi
  if [ -n "$PLAN" ]; then
    plan_info="$(az appservice plan list --query "[?name=='$PLAN'] | [0].[id, resourceGroup, location, reserved, sku.tier]" -o tsv)"
    [ -n "$plan_info" ] || die "there's no App Service plan called $PLAN."
    IFS=$'\t' read -r plan_id plan_rg plan_location plan_linux plan_tier <<<"$plan_info"
    [ "${plan_linux,,}" = "true" ] ||
      die "$PLAN is a Windows plan. Jarvis needs a Linux one, and Azure can't mix the two on one plan. Leave it blank for a new plan."
    [[ "$plan_tier" != "Free" && "$plan_tier" != "Shared" ]] || die "$PLAN is on the $plan_tier tier, which can't keep Jarvis running. Use a B1 or bigger."
    RG="$plan_rg"
    LOCATION="$(tr -d ' ' <<<"${plan_location,,}")"
    echo "Jarvis will share $PLAN, as its own web app in $RG. The apps already on the plan aren't changed, but they"
    echo "will share its memory and processor with Jarvis."
  elif [ "$(az group exists -n "$RG")" = "true" ]; then
    others="$(az resource list -g "$RG" --query "[?tags.app!='$TAG'].name" -o tsv | paste -sd ' ' -)"
    [ -z "$others" ] || die "resource group $RG already holds other things ($others). Use a new one, e.g.  RG=rg-salts-jarvis bash infra/deploy.sh"
  fi

  ask_password
  ask_claude

  local problem
  while :; do
    ask managers "Microsoft 365 addresses allowed to sign in, e.g. you and your business partner (space-separated, blank = password only): "
    problem=""
    for email in $managers; do  # check them all before creating anything
      problem="$(lookup_problem "$email")"
      [ -z "$problem" ] || break
    done
    [ -n "$problem" ] || break
    echo "$problem"
    echo "Try again."
  done
  staff_key="$(openssl rand -hex 12)"

  echo
  echo "Ready to create:"
  if [ -n "$plan_id" ]; then
    echo "  - web app $APP_NAME on the existing plan $PLAN, in resource group $RG"
  else
    echo "  - resource group $RG with a new Linux B1 plan ($APP_NAME-plan) and web app $APP_NAME, in $LOCATION"
  fi
  echo "  - a storage account for Jarvis's archive"
  echo "  - sign-in: ${managers:-password only}"
  echo "Nothing that already exists is changed."
  local go
  ask go "Carry on? (y/n): "
  [[ "$go" =~ ^[Yy] ]] || die "nothing was created."

  local params=(appName="$APP_NAME" location="$LOCATION" ownerPassword="$owner_password" staffReportKey="$staff_key")
  if [ -n "$claude_token" ]; then params+=(claudeCodeOauthToken="$claude_token"); else params+=(anthropicApiKey="$anthropic_key"); fi
  if [ -n "$plan_id" ]; then
    params+=(existingPlanId="$plan_id")
  elif [ "$(az group exists -n "$RG")" != "true" ]; then  # a group left empty by an earlier try is reused
    az group create -n "$RG" -l "$LOCATION" --tags app="$TAG" -o none
  fi
  echo "Creating the web app and storage (a couple of minutes)..."
  local errors
  errors="$(mktemp)"
  if ! az deployment group create -g "$RG" -n "jarvis-$(date +%Y%m%d%H%M%S)" -f infra/main.bicep -o none \
    -p "${params[@]}" 2>"$errors"; then
    cat "$errors" >&2
    grep -q "SubscriptionIsOverQuotaForSku" "$errors" && die "your Azure subscription has no room for another B1 plan in $LOCATION
(see 'Current Limit' above). Nothing was created. Run it again in another UK region:
    LOCATION=ukwest PLAN= bash infra/deploy.sh
or ask Azure for more: in the portal search for 'Quotas', open App Service, and request a higher B1 limit."
    die "Azure couldn't create Jarvis (details above). Nothing else was changed."
  fi
  rm -f "$errors"
  deploy_code

  url="https://$(az webapp show -g "$RG" -n "$APP_NAME" --query defaultHostName -o tsv)"
  echo
  echo "Jarvis:              $url  (password: the one you just chose)"
  echo "Staff report link:   $url/report?key=$staff_key"
  echo "Add your other keys: fill in a copy of .env.example, then  bash infra/deploy.sh settings <that file>"
  echo
  if [ -n "$managers" ]; then
    echo "Switching on Microsoft sign-in (if this fails, Jarvis still works with the password;"
    echo "retry with:  bash infra/deploy.sh signin $managers)"
    # shellcheck disable=SC2086  # one argument per address
    enable_signin $managers
  fi
}

push_settings() {
  local file="${1:-}" line key value pairs=() names=()
  [ -f "$file" ] || die "give the env file to copy, e.g.  bash infra/deploy.sh settings jarvis.env"
  find_jarvis_app
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*([A-Z][A-Z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    if [[ "$value" =~ ^\"([^\"]*)\" ]] || [[ "$value" =~ ^\'([^\']*)\' ]]; then
      value="${BASH_REMATCH[1]}"
    else
      value="$(sed -E 's/(^|[[:space:]]+)#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//' <<<"$value")"
    fi
    # Blank values stay unset; Azure-specific settings are managed by the template, sign-in by `signin`, and the
    # example file's placeholder secret key must never replace the random one.
    [ -n "$value" ] || continue
    case "$key" in
      DATA_DIR | PUBLIC_BASE_URL | PORT | HOST | WEBSITES_PORT | FORWARDED_ALLOW_IPS | JARVIS_SECRET_KEY) continue ;;
      MANAGER_EMAILS | "$SECRET_SETTING") continue ;;
    esac
    if [[ "$value" == you@* ]]; then
      echo "Skipped $key: it still has the example address ($value)."
      continue
    fi
    pairs+=("$key=$value")
    names+=("$key")
  done <"$file"
  [ "${#pairs[@]}" -gt 0 ] || die "no filled-in values found in $file."
  az webapp config appsettings set -g "$RG" -n "$APP_NAME" --settings "${pairs[@]}" -o none
  echo "Saved ${#names[@]} settings (Jarvis restarts to pick them up): ${names[*]}"
}

case "${1:-setup}" in
  setup) setup ;;
  update) deploy_code ;;
  settings) push_settings "${2:-}" ;;
  adopt) adopt "${2:-}" "${3:-}" ;;
  signin) shift; enable_signin "$@" ;;
  token) set_token ;;
  voice) setup_voice ;;
  voicetest) voice_test ;;
  m365) m365 ;;
  secret) set_secret "${2:-}" ;;
  *) die "unknown command '$1'. Use: bash infra/deploy.sh [setup|update|settings FILE|adopt NAME [GROUP]|signin EMAIL...|token|voice|voicetest|m365|secret NAME]" ;;
esac
