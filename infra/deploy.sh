#!/usr/bin/env bash
# Put Jarvis on Azure from Azure Cloud Shell (portal.azure.com, the >_ icon, Bash).
#
#   bash infra/deploy.sh                      first time: sets up Jarvis (sharing an existing plan if you pick one),
#                                             uploads the code and, if you name people, turns on Microsoft sign-in
#   bash infra/deploy.sh update               upload the latest Jarvis code (settings are kept)
#   bash infra/deploy.sh settings FILE        copy the filled-in values from an env file (like .env.example) into the app
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
  local found tag=""
  found="$(az webapp list --query "[?name=='$APP_NAME'] | [0].[resourceGroup, tags.app]" -o tsv)"
  [ -n "$found" ] || die "there's no web app called $APP_NAME. Check APP_NAME, or run the first-time setup."
  IFS=$'\t' read -r RG tag <<<"$found"
  [ "$tag" = "$TAG" ] || die "$APP_NAME isn't Jarvis (it has no app=jarvis tag), so it's been left alone."
}

deploy_code() {
  find_jarvis_app
  local tmp
  tmp="$(mktemp -d)"
  zip -qr "$tmp/jarvis.zip" jarvis knowledge requirements.txt ./*.yaml -x "*/__pycache__/*"
  echo "Uploading Jarvis to $APP_NAME. Azure installs the packages, which takes about 5 minutes..."
  if ! az webapp deploy -g "$RG" -n "$APP_NAME" --src-path "$tmp/jarvis.zip" --type zip -o none; then
    echo "Azure is still starting it, or the build failed. Watch the log with:  az webapp log tail -g $RG -n $APP_NAME"
  fi
  rm -rf "$tmp"
}

# Entra object id for a sign-in address, or stop with an explanation.
user_id() {
  local id
  id="$(az ad user show --id "$1" --query id -o tsv 2>/dev/null || true)"
  [ -n "$id" ] || die "$1 isn't an account in this Azure directory. Check it's their Microsoft 365 sign-in address.
If your Azure subscription was set up under a different Microsoft account from your Microsoft 365, sign-in can't
use your 365 accounts - leave the list blank and use the Jarvis password instead."
  echo "$id"
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
    read -rp "Plan to share (blank = a new B1 plan just for Jarvis, about £10 a month): " PLAN
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

  read -rsp "Choose a password for the Jarvis display: " owner_password; echo
  read -rsp "Type it again: " confirm; echo
  [ -n "$owner_password" ] && [ "$owner_password" = "$confirm" ] || die "the passwords didn't match."
  read -rsp "Claude token from 'claude setup-token' (leave blank to use an API key instead): " claude_token; echo
  anthropic_key=""
  if [ -z "$claude_token" ]; then
    read -rsp "Anthropic API key: " anthropic_key; echo
    [ -n "$anthropic_key" ] || die "Jarvis needs either the Claude token or an API key."
  fi
  read -rp "Microsoft 365 addresses allowed to sign in, e.g. you and your business partner (space-separated, blank = password only): " managers
  for email in $managers; do user_id "$email" >/dev/null; done  # check them all before creating anything
  staff_key="$(openssl rand -hex 12)"

  local params=(appName="$APP_NAME" location="$LOCATION" ownerPassword="$owner_password" staffReportKey="$staff_key")
  if [ -n "$claude_token" ]; then params+=(claudeCodeOauthToken="$claude_token"); else params+=(anthropicApiKey="$anthropic_key"); fi
  if [ -n "$plan_id" ]; then
    params+=(existingPlanId="$plan_id")
  else
    az group create -n "$RG" -l "$LOCATION" --tags app="$TAG" -o none
  fi
  echo "Creating the web app and storage (a couple of minutes)..."
  az deployment group create -g "$RG" -n "jarvis-$(date +%Y%m%d%H%M%S)" -f infra/main.bicep -o none -p "${params[@]}"
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
  signin) shift; enable_signin "$@" ;;
  *) die "unknown command '$1'. Use: bash infra/deploy.sh [setup|update|settings FILE|signin EMAIL...]" ;;
esac
