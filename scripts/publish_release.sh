#!/usr/bin/env bash
#
# publish_release.sh — konsistente GitHub-Releases fuer
# tkarle/hass-becker-component-plus-pybecker
#
# Behebt zwei wiederkehrende Probleme aus PROJECT_STATE.md:
#   - Bug D: literale "\n\n" in Release-Notes statt echter Zeilenumbrueche,
#     weil Notes bisher als Inline-String an `gh release create --notes "..."`
#     uebergeben wurden. Dieses Skript verlangt stattdessen eine Notes-Datei
#     (--notes-file), damit echte Newlines garantiert erhalten bleiben.
#   - Titel-Formatierung driftete von "v0.4.0-beta.2 – Safe UI hub migration"
#     bis hin zu Titeln ganz ohne Versionsnummer (beta.9, beta.10). Dieses
#     Skript erzwingt das Format "<tag> – <Kurzbeschreibung>".
#
# Ausserdem: Warnung beim Uebergang von einstelliger auf zweistellige
# Beta-Nummer (z. B. beta.9 -> beta.10), weil das bisher beobachtete
# HACS-Problem ("last_version" bleibt auf der alten Version stehen) mit
# einem simplen String-Vergleich von Versionsnummern zusammenhaengt
# (0.4.0-beta.9 > 0.4.0-beta.10 als reiner Zeichenkettenvergleich).
#
# Voraussetzung: GitHub CLI (`gh`) installiert und eingeloggt
# (`gh auth login`), im Repo-Root ausgefuehrt.
#
# Verwendung:
#   ./publish_release.sh v0.4.0-beta.11 "Kurzbeschreibung" notes.md [--stable]
#
# Beispiel:
#   ./publish_release.sh v0.4.0-beta.11 "Fix double-up regression" \
#       release-notes/beta11.md
#
# Ohne --stable wird das Release automatisch als "Pre-release" markiert,
# sobald der Tag "-beta.", "-alpha." oder "-rc." enthaelt.

set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <tag, z.B. v0.4.0-beta.11> <Kurzbeschreibung> <notes.md-Datei> [--stable]" >&2
  exit 1
fi

TAG="$1"
SHORT_DESC="$2"
NOTES_FILE="$3"
STABLE_FLAG="${4:-}"

# --- Vorab-Checks -----------------------------------------------------

if ! command -v gh >/dev/null 2>&1; then
  echo "Fehler: 'gh' (GitHub CLI) ist nicht installiert." >&2
  exit 1
fi

if [[ ! -f "$NOTES_FILE" ]]; then
  echo "Fehler: Notes-Datei '$NOTES_FILE' nicht gefunden." >&2
  echo "Lege die Release-Notes als echte Markdown-Datei an (mit echten" >&2
  echo "Zeilenumbruechen) statt sie als Inline-String zu uebergeben." >&2
  exit 1
fi

if [[ ! "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z]+\.[0-9]+)?$ ]]; then
  echo "Warnung: Tag '$TAG' entspricht nicht dem erwarteten Schema" >&2
  echo "  vX.Y.Z oder vX.Y.Z-beta.N / -alpha.N / -rc.N" >&2
  read -r -p "Trotzdem fortfahren? [y/N] " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || exit 1
fi

# --- manifest.json auf Konsistenz pruefen -----------------------------

MANIFEST="custom_components/becker/manifest.json"
if [[ -f "$MANIFEST" ]]; then
  MANIFEST_VERSION=$(grep -o '"version":[[:space:]]*"[^"]*"' "$MANIFEST" | sed -E 's/.*"([^"]+)"$/\1/')
  EXPECTED_VERSION="${TAG#v}"
  if [[ "$MANIFEST_VERSION" != "$EXPECTED_VERSION" ]]; then
    echo "Fehler: manifest.json Version ist '$MANIFEST_VERSION', erwartet '$EXPECTED_VERSION'." >&2
    echo "Bitte $MANIFEST vor dem Release aktualisieren und committen." >&2
    exit 1
  fi
else
  echo "Warnung: $MANIFEST nicht gefunden, ueberspringe Versions-Check." >&2
fi

# --- Warnung bei einstellig -> zweistellig (bekanntes HACS-Problem) ---

if [[ "$TAG" =~ -beta\.([0-9]+)$ ]]; then
  BETA_NUM="${BASH_REMATCH[1]}"
  PREV_NUM=$((BETA_NUM - 1))
  if [[ ${#BETA_NUM} -gt ${#PREV_NUM} ]]; then
    cat >&2 <<EOF

WARNUNG: Uebergang von "beta.${PREV_NUM}" auf "beta.${BETA_NUM}" (einstellig
-> zweistellig). Reine String-Vergleiche sortieren "beta.${PREV_NUM}" faelschlich
als "groesser" als "beta.${BETA_NUM}" (z.B. HACS-Versionsvergleich). Betroffene
Nutzer muessen die Version in HACS ggf. manuell auswaehlen (Download-Dialog ->
"Benoetigst du eine andere Version?"). Erwaege stattdessen z. B. einen Sprung
auf eine neue Minor-/Patch-Basis (0.4.1-beta.1), um das zu vermeiden.

EOF
    read -r -p "Trotzdem mit '$TAG' fortfahren? [y/N] " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || exit 1
  fi
fi

# --- Titel zusammensetzen ----------------------------------------------

TITLE="${TAG} – ${SHORT_DESC}"

PRERELEASE_ARGS=()
if [[ "$STABLE_FLAG" != "--stable" ]]; then
  if [[ "$TAG" =~ -(beta|alpha|rc)\. ]]; then
    PRERELEASE_ARGS=(--prerelease)
  fi
fi

echo "Erstelle Release:"
echo "  Tag:        $TAG"
echo "  Titel:      $TITLE"
echo "  Notes-Datei: $NOTES_FILE"
echo "  Pre-release: $([[ ${#PRERELEASE_ARGS[@]} -gt 0 ]] && echo ja || echo nein)"
echo
read -r -p "Release jetzt auf GitHub veroeffentlichen? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Abgebrochen."; exit 1; }

gh release create "$TAG" \
  --title "$TITLE" \
  --notes-file "$NOTES_FILE" \
  "${PRERELEASE_ARGS[@]}"

echo "Fertig: $TAG veroeffentlicht."
