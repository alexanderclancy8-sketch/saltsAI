#!/usr/bin/env bash
# Put Jarvis on Azure from Azure Cloud Shell (portal.azure.com, the >_ icon, Bash).
#
#   bash infra/deploy.sh                 first time: creates Jarvis's own resource group, plan, web app and storage,
#                                        then uploads the code
#   bash infra/deploy.sh update          upload the latest Jarvis code (settings are kept)
#   bash infra/deploy.sh settings FILE   copy the filled-in values from an env file (like .env.example) into the app
#
# Change the defaults with e.g.  APP_NAME=salts-jarvis-2 RG=rg-salts-jarvis bash infra/deploy.sh
#
# Jarvis gets its own resource group and App Service plan, and everything it creates is tagged app=jarvis. The
# script stops if the name or resource group is already used by anything else, and it only ever uploads code to a
# web app tagged app=jarvis, so it cannot overwrite Salts FSM or any other app in your subscription.
set -euo pipefail

APP_NAME="${APP_NAME:-salts-jarvis}"
RG="${RG:-rg-jarvis}"
LOCATION="${LOCATION:-uksouth}"
TAG="jarvis"
cd "$(dirname "$0")/.."

die() { echo "Stopped: $*" >&2; exit 1; }

command -v az >/dev/null || die "the Azure CLI isn't here. Run this in Azure Cloud Shell (portal.azure.com, the >_ icon)."

# Resource group of the web app called $APP_NAME in this subscription, if there is one.
app_group() { az webapp list --query "[?name=='$APP_NAME'].resourceGroup | [0]" -o tsv; }

require_jarvis_app() {
  local tag
  tag="$(az webapp show -g "$RG" -n "$APP_NAME" --query "tags.app" -o tsv 2>/dev/null || true)"
  [ "$tag" = "$TAG" ] || die "there's no Jarvis web app called $APP_NAME in $RG. Check APP_NAME and RG, or run the first-time setup."
}

deploy_code() {
  require_jarvis_app
  local tmp
  tmp="$(mktemp -d)"
  zip -qr "$tmp/jarvis.zip" jarvis knowledge requirements.txt ./*.yaml -x "*/__pycache__/*"
  echo "Uploading Jarvis to $APP_NAME. Azure installs the packages, which takes about 5 minutes..."
  if ! az webapp deploy -g "$RG" -n "$APP_NAME" --src-path "$tmp/jarvis.zip" --type zip -o none; then
    echo "Azure is still starting it, or the build failed. Watch the log with:  az webapp log tail -g $RG -n $APP_NAME"
  fi
  rm -rf "$tmp"
}

setup() {
  local existing others owner_password confirm claude_token anthropic_key staff_key url
  existing="$(app_group)"
  if [ -n "$existing" ]; then
    [ "$(az webapp show -g "$existing" -n "$APP_NAME" --query "tags.app" -o tsv)" = "$TAG" ] &&
      die "Jarvis is already set up as $APP_NAME in $existing. To upload new code use:  bash infra/deploy.sh update"
    die "a web app called $APP_NAME already exists and it isn't Jarvis. Pick another name, e.g.  APP_NAME=salts-jarvis-2 bash infra/deploy.sh"
  fi
  if [ "$(az group exists -n "$RG")" = "true" ]; then
    others="$(az resource list -g "$RG" --query "[?tags.app!='$TAG'].name" -o tsv | paste -sd ' ' -)"
    [ -z "$others" ] || die "resource group $RG already holds other things ($others). Use a new one, e.g.  RG=rg-salts-jarvis bash infra/deploy.sh"
  fi

  echo "Setting up Jarvis as $APP_NAME in resource group $RG ($LOCATION)."
  echo "It gets its own resource group and plan; nothing else in your subscription is changed."
  read -rsp "Choose a password for the Jarvis display: " owner_password; echo
  read -rsp "Type it again: " confirm; echo
  [ -n "$owner_password" ] && [ "$owner_password" = "$confirm" ] || die "the passwords didn't match."
  read -rsp "Claude token from 'claude setup-token' (leave blank to use an API key instead): " claude_token; echo
  anthropic_key=""
  if [ -z "$claude_token" ]; then
    read -rsp "Anthropic API key: " anthropic_key; echo
    [ -n "$anthropic_key" ] || die "Jarvis needs either the Claude token or an API key."
  fi
  staff_key="$(openssl rand -hex 12)"
  local params=(appName="$APP_NAME" ownerPassword="$owner_password" staffReportKey="$staff_key")
  if [ -n "$claude_token" ]; then params+=(claudeCodeOauthToken="$claude_token"); else params+=(anthropicApiKey="$anthropic_key"); fi

  az group create -n "$RG" -l "$LOCATION" --tags app="$TAG" -o none
  echo "Creating the web app and storage (a couple of minutes)..."
  az deployment group create -g "$RG" -f infra/main.bicep -o none -p "${params[@]}"
  deploy_code

  url="https://$(az webapp show -g "$RG" -n "$APP_NAME" --query defaultHostName -o tsv)"
  echo
  echo "Jarvis:              $url  (sign in with the password you just chose)"
  echo "Staff report link:   $url/report?key=$staff_key"
  echo "Add your other keys: fill in a copy of .env.example, then  bash infra/deploy.sh settings <that file>"
}

push_settings() {
  local file="${1:-}" line key value pairs=() names=()
  [ -f "$file" ] || die "give the env file to copy, e.g.  bash infra/deploy.sh settings jarvis.env"
  require_jarvis_app
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
    # Blank values stay unset; Azure-specific settings are managed by the template, and the example
    # file's placeholder secret key must never replace the random one.
    [ -n "$value" ] || continue
    case "$key" in
      DATA_DIR | PUBLIC_BASE_URL | PORT | HOST | WEBSITES_PORT | FORWARDED_ALLOW_IPS | JARVIS_SECRET_KEY) continue ;;
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
  *) die "unknown command '$1'. Use: bash infra/deploy.sh [setup|update|settings FILE]" ;;
esac
