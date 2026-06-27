#!/usr/bin/env python3
"""
Artemis Planning — Surveillance Gmail via API Google
Fonctionne en local ET sur GitHub Actions (24h/24)
"""

import os
import json
import base64
import time
import re
import requests
from datetime import datetime, timezone

# ─── CONFIGURATION ─────────────────────────────────────────────────────────────
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN_SECRET", "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")
GITHUB_USER   = "littlecrusher-ops"
GITHUB_REPO   = "Artemis-planning"
DEMANDES_FILE = "demandes.json"
CHECK_INTERVAL = 5

SUBJECT_KEYWORDS = ["indisponibilit", "cong", "repos compensateur", "rc", "demande"]

# ─── GMAIL API AUTH ────────────────────────────────────────────────────────────
def get_gmail_service():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    SCOPES = ['https://www.googleapis.com/auth/gmail.readonly',
              'https://www.googleapis.com/auth/gmail.modify']

    creds = None
    script_dir = os.path.dirname(os.path.abspath(__file__))
    token_path = os.path.join(script_dir, 'token.json')
    creds_path = os.path.join(script_dir, 'credentials.json')

    # Sur GitHub Actions : lire token.json depuis la variable d'environnement
    token_json_env = os.environ.get("TOKEN_JSON")
    if token_json_env:
        with open(token_path, 'w') as f:
            f.write(token_json_env)

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, 'w') as f:
                f.write(creds.to_json())
        else:
            if not os.path.exists(creds_path):
                print("  ERREUR : credentials.json introuvable !")
                exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            flow.redirect_uri = 'urn:ietf:wg:oauth:2.0:oob'
            auth_url, _ = flow.authorization_url()
            print('\nOuvre ce lien dans ton navigateur :')
            print(auth_url)
            code = input('\nColle le code ici : ')
            flow.fetch_token(code=code)
            creds = flow.credentials
            with open(token_path, 'w') as f:
                f.write(creds.to_json())

    return build('gmail', 'v1', credentials=creds)

# ─── LECTURE MAILS ─────────────────────────────────────────────────────────────
def get_mail_body(service, msg_id):
    try:
        msg = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
        payload = msg.get('payload', {})

        def extract_text(part):
            if part.get('mimeType') == 'text/plain':
                data = part.get('body', {}).get('data', '')
                if data:
                    return base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
            if part.get('mimeType') == 'text/html':
                data = part.get('body', {}).get('data', '')
                if data:
                    html = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
                    return re.sub(r'<[^>]+>', ' ', html)
            for p in part.get('parts', []):
                result = extract_text(p)
                if result:
                    return result
            return ''

        body = extract_text(payload)
        subject = ''
        for h in msg.get('payload', {}).get('headers', []):
            if h['name'] == 'Subject':
                subject = h['value']
                break
        return subject, body, msg_id
    except Exception as e:
        print(f"  Erreur lecture mail: {e}")
        return '', '', msg_id

def fetch_new_guardtek_mails(service, already_processed):
    try:
        query = 'is:unread subject:Demande'
        results = service.users().messages().list(userId='me', q=query).execute()
        messages = results.get('messages', [])

        nouvelles = []
        for m in messages:
            mid = m['id']
            if mid in already_processed:
                continue
            subject, body, msg_id = get_mail_body(service, mid)
            subject_lower = subject.lower()
            if not any(kw in subject_lower for kw in SUBJECT_KEYWORDS):
                continue
            demande = parse_demande(body, subject, msg_id)
            if demande:
                nouvelles.append(demande)
                print(f"  Nouvelle demande : {demande['demandeur']} - {demande['motif']} - {demande['dates_texte']}")
            service.users().messages().modify(
                userId='me', id=mid,
                body={'removeLabelIds': ['UNREAD']}
            ).execute()
        return nouvelles
    except Exception as e:
        print(f"  Erreur recherche mails: {e}")
        return []

# ─── PARSING ───────────────────────────────────────────────────────────────────
def parse_demande(body, subject, msg_id):
    # Nom du demandeur
    demandeur = ""
    for p in [
        r'Demandeur\s*[:\-]?\s*(?:Mr|Mme|M\.)?\s*([A-Z][A-Z\s]+)',
        r'D.clar.\s+par\s+([A-Za-z\s]+)',
        r'Mr\s+([A-Z]+)',
    ]:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            demandeur = m.group(1).strip()
            break

    # Motif — ordre important : indispo en premier !
    motif = "indisponibilite"
    body_lower = body.lower()
    
    # Chercher la valeur apres "indisponibilite /conges/ Repos compensateur :"
    motif_match = re.search(
        r'indisponibilit[^\n:]*[:\s]+([^\n]+)',
        body, re.IGNORECASE
    )
    if motif_match:
        valeur = motif_match.group(1).strip().lower()
        if 'indisponibilit' in valeur:
            motif = "indisponibilite"
        elif 'repos compensateur' in valeur or ' rc' in valeur:
            motif = "repos_compensateur"
        elif 'cong' in valeur:
            motif = "conge"
    else:
        # Fallback : chercher dans tout le corps
        if 'indisponibilit' in body_lower:
            motif = "indisponibilite"
        elif 'repos compensateur' in body_lower:
            motif = "repos_compensateur"
        elif 'conge' in body_lower or 'congé' in body_lower:
            motif = "conge"

    # Dates — chercher "Date souhaitee : X mois YYYY" en priorite
    dates_texte = ""
    date_debut = None
    date_fin = None

    # Pattern "X mois YYYY" (ex: "8 août 2026")
    m = re.search(
        r'[Dd]ate\s+souhait[^\n:]*[:\s]+(?:le\s+)?(\d{1,2})\s+([a-z\u00e0-\u00ff]+)\s*(\d{4})?',
        body, re.IGNORECASE
    )
    if m:
        jour = m.group(1)
        mois_str = m.group(2)
        annee_str = m.group(3)
        mn = mois_to_num(mois_str)
        annee = int(annee_str) if annee_str else datetime.now().year
        if mn:
            date_debut = f"{annee}-{mn:02d}-{int(jour):02d}"
            date_fin = date_debut
            dates_texte = f"le {jour} {mois_str} {annee}"

    # Pattern "du X au Y mois YYYY"
    if not date_debut:
        m = re.search(
            r'du\s+(\d{1,2})\s+au\s+(\d{1,2})\s+([a-z\u00e0-\u00ff]+)\s*(\d{4})?',
            body, re.IGNORECASE
        )
        if m:
            j1, j2, mois_str = m.group(1), m.group(2), m.group(3)
            annee_str = m.group(4)
            mn = mois_to_num(mois_str)
            annee = int(annee_str) if annee_str else datetime.now().year
            if mn:
                date_debut = f"{annee}-{mn:02d}-{int(j1):02d}"
                date_fin   = f"{annee}-{mn:02d}-{int(j2):02d}"
                dates_texte = f"du {j1} au {j2} {mois_str} {annee}"

    # Pattern "le X mois YYYY"
    if not date_debut:
        m = re.search(
            r'[Ll]e\s+(\d{1,2})\s+([a-z\u00e0-\u00ff]+)\s*(\d{4})?',
            body, re.IGNORECASE
        )
        if m:
            jour = m.group(1)
            mois_str = m.group(2)
            annee_str = m.group(3)
            mn = mois_to_num(mois_str)
            annee = int(annee_str) if annee_str else datetime.now().year
            if mn:
                date_debut = f"{annee}-{mn:02d}-{int(jour):02d}"
                date_fin = date_debut
                dates_texte = f"le {jour} {mois_str} {annee}"

    if not demandeur and not dates_texte:
        return None

    return {
        "id": msg_id,
        "demandeur": demandeur or "Inconnu",
        "motif": motif,
        "dates_texte": dates_texte or "Date non detectee",
        "date_debut": date_debut,
        "date_fin": date_fin,
        "statut": "en_attente",
        "recu_le": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }

def mois_to_num(mois):
    table = {
        "janvier":1,"fevrier":2,"février":2,"mars":3,"avril":4,
        "mai":5,"juin":6,"juillet":7,"aout":8,"août":8,
        "septembre":9,"octobre":10,"novembre":11,"decembre":12,"décembre":12
    }
    return table.get(mois.lower().strip())

# ─── GITHUB ────────────────────────────────────────────────────────────────────
def gh_headers():
    return {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}

def gh_get_sha(filename):
    url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{filename}"
    try:
        r = requests.get(url, headers=gh_headers())
        if r.status_code == 200:
            return r.json().get('sha')
    except: pass
    return None

def gh_push(filename, content_str, message):
    url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{filename}"
    b64 = base64.b64encode(content_str.encode('utf-8')).decode('ascii')
    sha = gh_get_sha(filename)
    payload = {"message": message, "content": b64, "branch": "main"}
    if sha: payload["sha"] = sha
    try:
        r = requests.put(url, headers=gh_headers(), json=payload)
        return r.status_code in (200, 201)
    except: return False

def gh_load(filename):
    url = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/{filename}?t={int(time.time())}"
    try:
        r = requests.get(url)
        if r.ok: return r.json()
    except: pass
    return None

def push_demandes(nouvelles):
    existing = gh_load(DEMANDES_FILE) or {"demandes": [], "processed_ids": []}
    processed = existing.get("processed_ids", [])
    demandes = existing.get("demandes", [])
    for d in nouvelles:
        if d["id"] not in processed:
            demandes.append(d)
            processed.append(d["id"])
    data = {"demandes": demandes, "processed_ids": processed,
            "updated_at": datetime.now(timezone.utc).isoformat()}
    ok = gh_push(DEMANDES_FILE, json.dumps(data, ensure_ascii=False, indent=2),
                 f"Nouvelles demandes {datetime.now().strftime('%d/%m %H:%M')}")
    return ok, processed

# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("╔══════════════════════════════════════════╗")
    print("║  Artemis — Surveillance Gmail Guardtek   ║")
    print("╚══════════════════════════════════════════╝")

    print("\n  Connexion a Gmail...")
    service = get_gmail_service()
    print("  Connecte !")

    processed_ids = []
    existing = gh_load(DEMANDES_FILE)
    if existing:
        processed_ids = existing.get("processed_ids", [])

    on_github_actions = os.environ.get("GITHUB_ACTIONS") == "true"

    if on_github_actions:
        print("  Mode GitHub Actions - execution unique")
        now = datetime.now().strftime("%H:%M")
        print(f"  [{now}] Verification Gmail...")
        nouvelles = fetch_new_guardtek_mails(service, processed_ids)
        if nouvelles:
            print(f"  {len(nouvelles)} nouvelle(s) demande(s) !")
            ok, _ = push_demandes(nouvelles)
            if ok: print("  Publie sur GitHub !")
        else:
            print("  Aucune nouvelle demande")
    else:
        print(f"  Surveillance demarree - verification toutes les {CHECK_INTERVAL} minutes\n")
        while True:
            now = datetime.now().strftime("%H:%M")
            print(f"  [{now}] Verification Gmail...")
            nouvelles = fetch_new_guardtek_mails(service, processed_ids)
            if nouvelles:
                print(f"  {len(nouvelles)} nouvelle(s) demande(s) !")
                ok, processed_ids = push_demandes(nouvelles)
                if ok: print("  Publie sur GitHub !")
            else:
                print("  Aucune nouvelle demande")
            from datetime import timedelta
            next_check = datetime.now() + timedelta(minutes=CHECK_INTERVAL)
            print(f"  Prochaine verification : {next_check.strftime('%H:%M')}\n")
            time.sleep(CHECK_INTERVAL * 60)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Surveillance arretee.")
