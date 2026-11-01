#!/usr/bin/env python3
"""
Neotopia Account & TempMail All-in-One Tool
- Dynamic DB_FILE path (automatically in the same directory as script)
- Generates tempmail accounts with random email & natural display names
- Silent magic link authentication -> extracts refreshToken & apiKey -> saves to accounts.json
- Full onboarding via headless Chromium: sign in through the magic link,
  pick an in-game username, skip the tutorial, then export a bot-ready account.
"""

import sys
import os
import json
import random
import string
import time
import urllib.request
import urllib.parse
import requests
import re

MAIL_TM_API = "https://api.mail.tm"
# Automatically set accounts.json in the same folder as this script
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_PASSWORD = "TempPassword123!"
FIREBASE_KEY = "AIzaSyBkFnfnk2i4moaF5KnzVAuEZE_6kSjqQds"

NAME_PREFIXES = ["riki", "soyu", "dori", "bintang", "ryan", "raka", "wisp", "reza", "kevin", "nova", "luna", "kai", "zara", "bayu", "dimas"]

CHROMIUM_CDP_PORT = 9333
CHROMIUM_BIN = "/data/data/com.termux/files/usr/bin/chromium-browser"


def load_accounts():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_account(address, password, token=None, display_name=None, refresh_token=None):
    accounts = load_accounts()
    entry = accounts.get(address, {
        "password": password,
        "token": token,
        "display_name": display_name,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
    })
    if token:
        entry["token"] = token
    if display_name:
        entry["display_name"] = display_name
    if refresh_token:
        entry["refresh_token"] = refresh_token
        entry["apiKey"] = FIREBASE_KEY
        entry["api_key"] = FIREBASE_KEY
    accounts[address] = entry
    with open(DB_FILE, "w") as f:
        json.dump(accounts, f, indent=2)

def save_bot_account(email, auth_data, display_name=None):
    """Write the auth result in the format expected by stay_world.py."""
    safe_email = re.sub(r"[^a-zA-Z0-9_.-]+", "_", email)
    path = os.path.join(BASE_DIR, f"guest_account_{safe_email}.json")
    account = {
        "api_key": FIREBASE_KEY,
        "id_token": auth_data["idToken"],
        "refresh_token": auth_data["refreshToken"],
        "uid": auth_data["localId"],
        "canonical_name": display_name or "",
        "type": "firebase_email_link",
        "platform": "desktop",
        "world": "778839",
    }
    with open(path, "w") as f:
        json.dump(account, f, indent=2)
    os.chmod(path, 0o600)
    return path

def generate_random_email():
    letters_digits = string.ascii_lowercase + string.digits
    user_part = "".join(random.choices(letters_digits, k=12))
    return f"{user_part}@maxxspace.com"

def generate_random_name():
    prefix = random.choice(NAME_PREFIXES)
    number = random.randint(100, 9999)
    return f"{prefix}{number}"

def create_mailtm_account(email, password):
    try:
        res = requests.post(f"{MAIL_TM_API}/accounts", json={
            "address": email,
            "password": password
        }, timeout=10)
        if res.status_code in [200, 201]:
            tok_res = requests.post(f"{MAIL_TM_API}/token", json={
                "address": email,
                "password": password
            }, timeout=10)
            if tok_res.status_code == 200:
                return tok_res.json().get("token")
    except Exception as e:
        print(f"Mail.tm error for {email}: {e}")
    return None

def request_magic_link(email):
    """Request a fresh Firebase magic link and return its URL."""
    url_send = f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={FIREBASE_KEY}"
    payload = {"requestType": "EMAIL_SIGNIN", "email": email, "continueUrl": "https://playneotopia.com/", "canHandleCodeInApp": True}
    req = urllib.request.Request(url_send, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)
    time.sleep(5)

def read_latest_magic_link(email, mail_tm_token):
    headers = {"Authorization": f"Bearer {mail_tm_token}"}
    res = requests.get(f"{MAIL_TM_API}/messages", headers=headers, timeout=10)
    msgs = res.json().get("hydra:member", [])
    if not msgs:
        return None
    msg_res = requests.get(f"{MAIL_TM_API}/messages/{msgs[0]['id']}", headers=headers, timeout=10)
    html_content = msg_res.json().get("html", [""])[0]
    match = re.search(r'href="([^"]*oobCode=[^"]+)"', html_content) or re.search(r'(https?://[^"\s]*oobCode=[^"\s]+)', html_content)
    if not match:
        return None
    return match.group(1).replace("&amp;", "&")

def exchange_magic_link(email, link):
    oobCode = re.search(r"oobCode=([a-zA-Z0-9_-]+)", link).group(1)
    url_signin = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithEmailLink?key={FIREBASE_KEY}"
    signin_payload = {"email": email, "oobCode": oobCode, "returnSecureToken": True}
    req = urllib.request.Request(url_signin, data=json.dumps(signin_payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())

# --- Headless Chromium onboarding (username + tutorial skip) ---

def _terminate_chromium_cdp():
    """Kill any running Chromium CDP process so the next run starts from a clean browser state."""
    import subprocess
    # The Chromium binary is actually "chrome"; match on the profile dir + port.
    result = subprocess.run(
        ["pkill", "-f", f"remote-debugging-port={CHROMIUM_CDP_PORT}"],
        capture_output=True,
    )
    result = subprocess.run(
        ["pkill", "-f", "chrome-profile-neo --remote-debugging-port"],
        capture_output=True,
    )
    time.sleep(3)


def start_chromium_cdp():
    """Start a fresh headless Chromium with remote debugging on CDP_PORT."""
    _terminate_chromium_cdp()
    # Remove stale SingletonLock that prevents Chromium from starting
    import subprocess
    profile_dir = os.path.join(BASE_DIR, "chrome-profile-neo")
    lock_file = os.path.join(profile_dir, "SingletonLock")
    if os.path.exists(lock_file) or os.path.islink(lock_file):
        try:
            os.remove(lock_file)
        except Exception:
            pass
    try:
        json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CHROMIUM_CDP_PORT}/json/version", timeout=2).read())
        print("  -> Chromium CDP already running, reusing")
        return
    except Exception:
        pass

    log_file = open(os.path.join(BASE_DIR, "chrome_cdp.log"), "ab")
    subprocess.Popen([
        CHROMIUM_BIN,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={CHROMIUM_CDP_PORT}",
        "--remote-allow-origins=*",
        "about:blank",
    ], stdout=log_file, stderr=log_file, start_new_session=True)
    log_file.close()
    for _ in range(20):
        time.sleep(1)
        try:
            json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CHROMIUM_CDP_PORT}/json/version", timeout=3).read())
            print("  -> Started headless Chromium on port", CHROMIUM_CDP_PORT)
            return
        except Exception:
            continue
    raise RuntimeError("Chromium CDP did not come up on port " + str(CHROMIUM_CDP_PORT))


class CdpBrowser:
    """Tiny wrapper around Chromium DevTools Protocol over WebSocket."""

    def __init__(self):
        import websocket
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CHROMIUM_CDP_PORT}/json", timeout=5).read())
        target = next((t for t in tabs if t["type"] == "page"), None)
        if not target:
            new = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{CHROMIUM_CDP_PORT}/json/new?about:blank", timeout=5).read())
            target = new
        self.ws = websocket.create_connection(target["webSocketDebuggerUrl"])
        self._id = 0

    def cmd(self, method, params=None):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            ev = json.loads(self.ws.recv())
            if ev.get("id") == self._id:
                return ev

    def get(self, expr):
        result = self.cmd("Runtime.evaluate", {"expression": expr})
        return result.get("result", {}).get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def complete_onboarding(browser, email, preferred_username, link):
    """Drive the 'Confirm Email' -> 'Choose your name' -> 'Skip' flow on the existing page."""
    browser.cmd("Page.enable")
    browser.cmd("Page.navigate", {"url": link})
    time.sleep(20)
    for attempt in range(3):
        body = browser.get("document.body ? document.body.innerText.slice(0, 500) : ''")
        print(f"  -> Page state (try {attempt+1}):", body.replace("\n", " | ")[:160])
        if "Confirm Email" in body or "different device" in body:
            break
        if "CHOOSE YOUR NAME" in body.upper() or "Pick a username" in body:
            break
        if "Game Already Open" in body:
            # A stale session from a previous tab is blocking; force reload and retry.
            print("  -> 'Game Already Open' detected; forcing reload")
            browser.cmd("Page.reload", {"ignoreCache": True})
            time.sleep(20)
            continue
        if "ACCOUNT CHECK" in body or "account group already has" in body.lower():
            # Account group limit reached; click TRY AGAIN or SIGN OUT
            print("  -> Account group limit reached, clicking TRY AGAIN")
            browser.cmd("Runtime.evaluate", {
                "expression": "Array.from(document.querySelectorAll('button')).find(b=>/TRY AGAIN/i.test(b.innerText))?.click() || Array.from(document.querySelectorAll('button')).find(b=>/SIGN OUT/i.test(b.innerText))?.click()"
            })
            time.sleep(10)
            continue
        if "JOIN" in body and "World" in body:
            # Already on the main dashboard (session reuse) - may still need username + tutorial
            break
        time.sleep(8)
    if "Confirm Email" in body or "different device" in body:
        print("  -> Confirming email to continue sign-in")
        browser.cmd("Runtime.evaluate", {
            "expression": (
                "var inp=document.querySelector('input');"
                "var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                f"s.call(inp, {json.dumps(email)});"
                "inp.dispatchEvent(new Event('input',{bubbles:true}));"
                "inp.dispatchEvent(new Event('change',{bubbles:true}))"
            )
        })
        time.sleep(1)
        browser.cmd("Runtime.evaluate", {
            "expression": "Array.from(document.querySelectorAll('button')).find(b=>/Continue/i.test(b.innerText))?.click()"
        })
        time.sleep(15)
        body = browser.get("document.body ? document.body.innerText.slice(0, 300) : ''")
        print("  -> After confirm:", body.replace("\n", " | ")[:150])

    # Now handle username selection. Could be on "CHOOSE YOUR NAME" screen OR on main dashboard with a modal.
    body = browser.get("document.body ? document.body.innerText.slice(0, 500) : ''")
    if "CHOOSE YOUR NAME" in body.upper() or "Pick a username" in body:
        print("  -> Picking username on 'CHOOSE YOUR NAME' screen:", preferred_username)
        _fill_username_and_start(browser, preferred_username)
        body = browser.get("document.body ? document.body.innerText.slice(0, 500) : ''")
    elif "JOIN" in body and "World" in body:
        # On main dashboard - check if there's a username input/modal
        has_username_input = browser.get("document.querySelector('input[placeholder*=\"name\" i], input[placeholder*=\"username\" i], input[id*=\"name\" i], input[class*=\"name\" i]') ? 'yes' : 'no'")
        if has_username_input == "yes":
            print("  -> Username input found on main dashboard, filling...")
            _fill_username_and_start(browser, preferred_username)
            body = browser.get("document.body ? document.body.innerText.slice(0, 500) : ''")
        else:
            print("  -> No username input found on dashboard, assuming already set")

    # If the tutorial screen appears, click Skip.
    body = browser.get("document.body ? document.body.innerText.slice(0, 500) : ''")
    if "Skip" in body and ("tap your fist" in body or "Welcome to Neotopia" in body or "tutorial" in body.lower()):
        print("  -> Tutorial screen detected, clicking Skip")
        browser.cmd("Runtime.evaluate", {
            "expression": "Array.from(document.querySelectorAll('button')).find(b=>/^Skip$/i.test(b.innerText.trim()))?.click()"
        })
        time.sleep(8)
        body = browser.get("document.body ? document.body.innerText.slice(0, 500) : ''")
        print("  -> After Skip:", body.replace("\n", " | ")[:150])

    if "JOIN" in body or "World" in body or "START" in body or "GO" in body or "HOME BASE" in body:
        return preferred_username
    print("  -> WARNING: did not land on main dashboard; proceeding with best effort")
    return preferred_username


def _fill_username_and_start(browser, preferred_username):
    """Fill username input and click START PLAYING (retry on taken)."""
    print("  -> Filling username:", preferred_username)
    browser.cmd("Runtime.evaluate", {
        "expression": (
            "var inp=document.querySelector('input[placeholder*=\"name\" i], input[placeholder*=\"username\" i], input[id*=\"name\" i], input[class*=\"name\" i], input');"
            "var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
            f"s.call(inp, {json.dumps(preferred_username)});"
            "inp.dispatchEvent(new Event('input',{bubbles:true}));"
            "inp.dispatchEvent(new Event('change',{bubbles:true}))"
        )
    })
    time.sleep(1)
    for _ in range(5):
        browser.cmd("Runtime.evaluate", {
            "expression": "Array.from(document.querySelectorAll('button')).find(b=>/START PLAYING/i.test(b.innerText))?.click()"
        })
        time.sleep(10)
        body = browser.get("document.body ? document.body.innerText.slice(0, 500) : ''")
        if "Skip" in body or "JOIN" in body or "World" in body:
            break
        if "temporarily unavailable" in body:
            print("  -> 'temporarily unavailable'; retrying with a unique name")
            preferred_username = preferred_username + str(int(time.time()))[-4:]
            browser.cmd("Runtime.evaluate", {
                "expression": (
                    "var inp=document.querySelector('input[placeholder*=\"name\" i], input[placeholder*=\"username\" i], input[id*=\"name\" i], input[class*=\"name\" i], input');"
                    "var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                    f"s.call(inp, {json.dumps(preferred_username)});"
                    "inp.dispatchEvent(new Event('input',{bubbles:true}));"
                    "inp.dispatchEvent(new Event('change',{bubbles:true}))"
                )
            })
        print("  -> After START PLAYING:", body.replace("\n", " | ")[:150])


def _extract_firebase_tokens(browser):
    """Extract Firebase auth tokens from the browser's localStorage while CDP is still open."""
    auth_data = None
    try:
        # Firebase stores auth data under keys like:
        #   firebase:authUser:<apiKey>:<appName>
        # Enumerate all localStorage keys and scan for one containing refreshToken/stsTokenManager.
        keys_json = browser.get("JSON.stringify(Object.keys(localStorage))")
        if not keys_json:
            return None
        keys = json.loads(keys_json)
        print(f"  -> localStorage keys ({len(keys)}): {keys[:20]}")
        for key in keys:
            if "firebase" not in key.lower() and "token" not in key.lower():
                continue
            raw = browser.get(f"localStorage.getItem({json.dumps(key)})")
            if not raw or "refresh" not in raw.lower():
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            # Firebase authUser format has stsTokenManager.refreshToken
            sts = data.get("stsTokenManager", {})
            refresh = (sts.get("refreshToken") or data.get("refreshToken")
                       or data.get("refresh_token") or "")
            idtok = (sts.get("accessToken") or data.get("idToken")
                     or data.get("id_token") or "")
            uid = data.get("uid") or data.get("localId") or ""
            if refresh:
                auth_data = {"idToken": idtok, "refreshToken": refresh, "localId": uid}
                print(f"  -> Token Firebase ditemukan di key: {key}")
                break
        if not auth_data:
            # Scan ALL localStorage values for refreshToken (broader search)
            print("  -> Scanning all localStorage values for refreshToken...")
            for key in keys:
                raw = browser.get(f"localStorage.getItem({json.dumps(key)})")
                if not raw:
                    continue
                if "refresh" in raw.lower() or "stsTokenManager" in raw:
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    sts = data.get("stsTokenManager", {})
                    refresh = (sts.get("refreshToken") or data.get("refreshToken")
                               or data.get("refresh_token") or "")
                    idtok = (sts.get("accessToken") or data.get("idToken")
                             or data.get("id_token") or "")
                    uid = data.get("uid") or data.get("localId") or ""
                    if refresh:
                        auth_data = {"idToken": idtok, "refreshToken": refresh, "localId": uid}
                        print(f"  -> Token Firebase ditemukan (full scan) di key: {key}")
                        break
        if not auth_data:
            # Try sessionStorage as well
            keys_json = browser.get("JSON.stringify(Object.keys(sessionStorage))")
            if keys_json:
                keys = json.loads(keys_json)
                for key in keys:
                    raw = browser.get(f"sessionStorage.getItem({json.dumps(key)})")
                    if not raw or "refresh" not in raw.lower():
                        continue
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    sts = data.get("stsTokenManager", {})
                    refresh = (sts.get("refreshToken") or data.get("refreshToken")
                               or data.get("refresh_token") or "")
                    idtok = (sts.get("accessToken") or data.get("idToken")
                             or data.get("id_token") or "")
                    uid = data.get("uid") or data.get("localId") or ""
                    if refresh:
                        auth_data = {"idToken": idtok, "refreshToken": refresh, "localId": uid}
                        print(f"  -> Token Firebase ditemukan di sessionStorage key: {key}")
                        break
        if not auth_data:
            # Try Firebase SDK directly - get current user tokens
            try:
                print("  -> Trying Firebase SDK getAuth().currentUser.getIdToken()...")
                token_from_sdk = browser.get("""
                    (async () => {
                        try {
                            const auth = window.firebase?.auth?.() || window.__FIREBASE_APP__?.auth?.();
                            if (!auth) return '';
                            const user = auth.currentUser;
                            if (!user) return '';
                            const idToken = await user.getIdToken();
                            return JSON.stringify({idToken: idToken, uid: user.uid, email: user.email});
                        } catch (e) {
                            return 'error:' + e.message;
                        }
                    })()
                """)
                print(f"  -> Firebase SDK result: {token_from_sdk[:200]}")
                if token_from_sdk and not token_from_sdk.startswith('error'):
                    data = json.loads(token_from_sdk)
                    idtok = data.get("idToken")
                    uid = data.get("uid")
                    if idtok:
                        # We still need refreshToken - try to get it from storage
                        pass
            except Exception as e:
                print(f"  -> Firebase SDK error: {e}")

            # DEBUG: dump ALL localStorage values to find tokens
            try:
                print("  -> DEBUG: scanning ALL localStorage values for tokens...")
                all_items = browser.get("""
                    (() => {
                        const out = {};
                        for (let i = 0; i < localStorage.length; i++) {
                            const key = localStorage.key(i);
                            const val = localStorage.getItem(key);
                            out[key] = val;
                        }
                        return JSON.stringify(out);
                    })()
                """)
                print(f"  -> DEBUG: localStorage dump length: {len(all_items) if all_items else 0}")
                if all_items:
                    data = json.loads(all_items)
                    for k, v in data.items():
                        vl = v.lower()
                        if 'refresh' in vl or 'ststokenmanager' in vl or 'accesstoken' in vl or 'idtoken' in vl or 'firebas' in vl or 'auth' in vl:
                            print(f"    -> CANDIDATE localStorage[{k}]: {v[:500]}")
                            try:
                                parsed = json.loads(v)
                                sts = parsed.get("stsTokenManager", {})
                                refresh = sts.get("refreshToken") or parsed.get("refreshToken")
                                idtok = sts.get("accessToken") or parsed.get("idToken")
                                uid = parsed.get("uid") or parsed.get("localId")
                                if refresh:
                                    auth_data = {"idToken": idtok, "refreshToken": refresh, "localId": uid}
                                    print(f"  -> Token Firebase ditemukan di localStorage[{k}]!")
                                    break
                            except Exception as e:
                                print(f"      -> Parse error: {e}")
                                pass
                        # Also print first 200 chars of ALL values to see what's there
                        if len(data) < 10:
                            print(f"    -> ALL[{k}]: {v[:200]}")
            except Exception as e:
                print(f"  -> Full localStorage scan error: {e}")

            # Try IndexedDB - Firebase often uses indexedDB for auth
            # First, discover ALL IndexedDB databases and their stores
            try:
                print("  -> Discovering ALL IndexedDB databases...")
                all_dbs_info = browser.get("""
                    (async () => {
                        const dbs = await indexedDB.databases();
                        const result = [];
                        for (const dbInfo of dbs) {
                            const dbName = dbInfo.name;
                            if (!dbName) continue;
                            try {
                                const db = await new Promise((resolve, reject) => {
                                    const req = indexedDB.open(dbName);
                                    req.onsuccess = () => resolve(req.result);
                                    req.onerror = () => reject(req.error);
                                    req.onupgradeneeded = () => reject(new Error('upgrade needed'));
                                });
                                result.push({
                                    name: dbName,
                                    stores: Array.from(db.objectStoreNames)
                                });
                                db.close();
                            } catch (e) {
                                result.push({name: dbName, stores: [], error: String(e)});
                            }
                        }
                        return JSON.stringify(result);
                    })()
                """)
                print(f"  -> IndexedDB databases: {all_dbs_info}")
            except Exception as e:
                print(f"  -> IndexedDB discovery error: {e}")

            # Try opening known Firebase IndexedDB names directly
            try:
                print("  -> Trying known Firebase IndexedDB names...")
                token_from_idb = browser.get("""
                    (async () => {
                        // Firebase auth IndexedDB names
                        const dbNames = [
                            'firebaseLocalStorageDb',
                            'firestore_localstorage',
                            'firebase-heartbeat-database',
                            'firebase-installations-database',
                            'firebase-auth',
                            'firestore-auth'
                        ];
                        for (const dbName of dbNames) {
                            try {
                                const db = await new Promise((resolve, reject) => {
                                    const req = indexedDB.open(dbName);
                                    req.onsuccess = () => resolve(req.result);
                                    req.onerror = () => reject(req.error);
                                    req.onupgradeneeded = () => reject(new Error('upgrade needed'));
                                });
                                for (const storeName of db.objectStoreNames) {
                                    try {
                                        const tx = db.transaction(storeName, 'readonly');
                                        const store = tx.objectStore(storeName);
                                        const cursorReq = store.openCursor();
                                        const result = await new Promise((resolve, reject) => {
                                            cursorReq.onsuccess = () => {
                                                const cursor = cursorReq.result;
                                                if (cursor) {
                                                    if (cursor.key && cursor.key.toString().includes('authUser') &&
                                                        cursor.value && cursor.value.stsTokenManager &&
                                                        cursor.value.stsTokenManager.refreshToken) {
                                                        db.close();
                                                        return resolve(JSON.stringify({source: dbName, store: storeName, key: cursor.key, data: cursor.value}));
                                                    }
                                                    cursor.continue();
                                                } else {
                                                    resolve('');
                                                }
                                            };
                                            cursorReq.onerror = () => reject(cursorReq.error);
                                        });
                                        if (result) {
                                            db.close();
                                            return result;
                                        }
                                    } catch (e) {}
                                }
                                db.close();
                            } catch (e) {}
                        }
                        return '';
                    })()
                """)
                if token_from_idb:
                    parsed = json.loads(token_from_idb)
                    data = parsed.get("data", {})
                    sts = data.get("stsTokenManager", {})
                    refresh = sts.get("refreshToken")
                    idtok = sts.get("accessToken")
                    uid = data.get("uid")
                    if refresh:
                        auth_data = {"idToken": idtok, "refreshToken": refresh, "localId": uid}
                        print(f"  -> Token Firebase ditemukan di IndexedDB ({parsed.get('source')}/{parsed.get('store')})!")
            except Exception as e:
                print(f"  -> IndexedDB extraction error: {e}")

            # Try navigating to Firebase auth domain and extracting from there
            if not auth_data:
                try:
                    print("  -> Navigating to Firebase auth domain to extract tokens...")
                    browser.cmd("Page.navigate", {"url": "https://neotopia-production.firebaseapp.com/"})
                    time.sleep(5)
                    # Now try IndexedDB on the auth domain
                    token_from_idb = browser.get("""
                        (async () => {
                            const dbNames = ['firebaseLocalStorageDb', 'firestore_localstorage'];
                            for (const dbName of dbNames) {
                                try {
                                    const db = await new Promise((resolve, reject) => {
                                        const req = indexedDB.open(dbName);
                                        req.onsuccess = () => resolve(req.result);
                                        req.onerror = () => reject(req.error);
                                    });
                                    for (const storeName of db.objectStoreNames) {
                                        try {
                                            const tx = db.transaction(storeName, 'readonly');
                                            const store = tx.objectStore(storeName);
                                            const cursorReq = store.openCursor();
                                            const result = await new Promise((resolve, reject) => {
                                                cursorReq.onsuccess = () => {
                                                    const cursor = cursorReq.result;
                                                    if (cursor) {
                                                        if (cursor.key && cursor.key.toString().includes('authUser') &&
                                                            cursor.value && cursor.value.stsTokenManager &&
                                                            cursor.value.stsTokenManager.refreshToken) {
                                                            db.close();
                                                            return resolve(JSON.stringify({source: dbName, store: storeName, key: cursor.key, data: cursor.value}));
                                                        }
                                                        cursor.continue();
                                                    } else {
                                                        resolve('');
                                                    }
                                                };
                                                cursorReq.onerror = () => reject(cursorReq.error);
                                            });
                                            if (result) {
                                                db.close();
                                                return result;
                                            }
                                        } catch (e) {}
                                    }
                                    db.close();
                                } catch (e) {}
                            }
                            return '';
                        })()
                    """)
                    if token_from_idb:
                        parsed = json.loads(token_from_idb)
                        data = parsed.get("data", {})
                        sts = data.get("stsTokenManager", {})
                        refresh = sts.get("refreshToken")
                        idtok = sts.get("accessToken")
                        uid = data.get("uid")
                        if refresh:
                            auth_data = {"idToken": idtok, "refreshToken": refresh, "localId": uid}
                            print(f"  -> Token Firebase ditemukan di IndexedDB (auth domain: {parsed.get('source')}/{parsed.get('store')})!")
                except Exception as e:
                    print(f"  -> Auth domain IndexedDB error: {e}")
    except Exception as e:
        print(f"  -> Error extracting Firebase tokens: {e}")
    return auth_data


def cmd_generate():
    try:
        jumlah_str = input("Masukkan JUMLAH akun yang ingin dibuat: ")
        jumlah = int(jumlah_str)
    except Exception:
        print("Input harus berupa angka.")
        return

    print(f"\nMembuat {jumlah} akun baru secara otomatis...")

    for i in range(1, jumlah + 1):
        email = generate_random_email()
        display_name = generate_random_name()
        password = DEFAULT_PASSWORD

        print(f"\n[{i}/{jumlah}] Email: {email} | Nama: {display_name}")

        token = create_mailtm_account(email, password)
        if not token:
            print(f"  -> Gagal membuat akun mail.tm")
            continue
        print(f"  -> Akun mail.tm berhasil dibuat.")

        save_account(email, password, token, display_name)
        print(f"  -> Tersimpan di {DB_FILE}")

        print("  -> Meminta magic link Firebase...")
        request_magic_link(email)
        link = None
        for _ in range(3):
            link = read_latest_magic_link(email, token)
            if link:
                break
            time.sleep(5)
        if not link:
            print("  -> Gagal mengambil magic link dari inbox Mail.tm")
            continue

        # Clear Chrome profile before each account to avoid account group limits
        import shutil
        profile_dir = os.path.join(BASE_DIR, "chrome-profile-neo")
        if os.path.exists(profile_dir):
            shutil.rmtree(profile_dir, ignore_errors=True)

        print("  -> Menjalankan onboarding di headless Chromium (sign-in, choose name, skip tutorial)...")
        try:
            start_chromium_cdp()
            browser = CdpBrowser()
        except Exception as e:
            print(f"  -> Gagal menjalankan Chromium: {e}")
            continue
        try:
            try:
                final_name = complete_onboarding(browser, email, display_name, link)
            except Exception as e:
                print(f"  -> Onboarding error: {e}")
                final_name = display_name

            # --- Extract Firebase auth tokens from browser BEFORE closing ---
            print("  -> Mengambil refresh token Firebase dari sesi browser...")
            auth_data = _extract_firebase_tokens(browser)
        except Exception as e:
            print(f"  -> Browser session error: {e}")
            auth_data = None
            final_name = display_name
        finally:
            try:
                browser.close()
            except Exception:
                pass

        # CRITICAL: After onboarding completes, request a FRESH magic link
        # (the original oobCode was consumed by the browser during onboarding)
        if not auth_data:
            print("  -> Token dari browser tidak ditemukan, request fresh magic link...")
            try:
                request_magic_link(email)
                time.sleep(5)
                link2 = read_latest_magic_link(email, token)
                if link2:
                    auth_data = exchange_magic_link(email, link2)
            except Exception as e2:
                print(f"  -> Fallback magic link gagal: {e2}")

        if not auth_data:
            print(f"  -> Gagal mendapatkan token (akun tetap tersimpan, coba manual nanti)")
            continue

        print("  -> Update display name Firebase bila diperlukan")
        try:
            url_update = f"https://identitytoolkit.googleapis.com/v1/accounts:update?key={FIREBASE_KEY}"
            update_payload = {"idToken": auth_data["idToken"], "displayName": final_name, "returnSecureToken": True}
            req_up = urllib.request.Request(url_update, data=json.dumps(update_payload).encode(), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req_up, timeout=10)
        except Exception as e:
            print(f"  -> update displayName tidak dijalankan: {e}")

        save_account(email, password, token=token, display_name=final_name, refresh_token=auth_data["refreshToken"])
        bot_account = save_bot_account(email, auth_data, display_name=final_name)
        print(f"[INFO] Auth + onboarding selesai. File bot siap pakai: {bot_account}")
        print("[INFO] Refresh token tidak ditampilkan di terminal.")

    print(f"\nSelesai! {jumlah} akun diproses.")

def cmd_get_credentials(email):
    accounts = load_accounts()
    if email not in accounts:
        print(f"Email {email} tidak ditemukan di {DB_FILE}")
        return

    # 1. Send OOB code
    request_magic_link(email)
    time.sleep(2)

    # 2. Check inbox
    token = accounts[email].get("token")
    headers = {"Authorization": f"Bearer {token}"}
    res = requests.get(f"{MAIL_TM_API}/messages", headers=headers, timeout=10)
    msgs = res.json().get("hydra:member", [])
    if not msgs:
        print("Belum ada pesan masuk di inbox.")
        return

    latest_msg = msgs[0]
    msg_res = requests.get(f"{MAIL_TM_API}/messages/{latest_msg['id']}", headers=headers, timeout=10)
    html_content = msg_res.json().get("html", [""])[0]

    match = re.search(r"oobCode=([a-zA-Z0-9_-]+)", html_content)
    if not match:
        print("Gagal mengekstrak oobCode dari email.")
        return
    oobCode = match.group(1)
    link = f"https://neotopia-production.firebaseapp.com/__/auth/action?apiKey={FIREBASE_KEY}&mode=signIn&oobCode={oobCode}"

    # 3. Onboard in browser (username + skip tutorial) so the account can join a world
    auth_data = None
    try:
        start_chromium_cdp()
        browser = CdpBrowser()
    except Exception as e:
        print(f"  -> Chromium tidak tersedia ({e}); melanjutkan tanpa onboarding web")
        browser = None
    if browser:
        try:
            preferred_name = accounts[email].get("display_name") or generate_random_name()
            complete_onboarding(browser, email, preferred_name, link)
            auth_data = _extract_firebase_tokens(browser)
        except Exception as e:
            print(f"  -> Onboarding web error: {e}")
        finally:
            try:
                browser.close()
            except Exception:
                pass

    # 4. Get tokens — from browser extraction or fresh magic link
    if not auth_data:
        try:
            request_magic_link(email)
            time.sleep(5)
            link2 = read_latest_magic_link(email, token) or link
            auth_data = exchange_magic_link(email, link2)
        except Exception as e:
            print(f"Gagal login: {e}")
            return

    try:
        display_name = accounts[email].get("display_name")
        if display_name:
            url_update = f"https://identitytoolkit.googleapis.com/v1/accounts:update?key={FIREBASE_KEY}"
            update_payload = {"idToken": auth_data["idToken"], "displayName": display_name, "returnSecureToken": True}
            req_up = urllib.request.Request(url_update, data=json.dumps(update_payload).encode(), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req_up, timeout=10)

        save_account(email, accounts[email]["password"], refresh_token=auth_data["refreshToken"])
        bot_account = save_bot_account(email, auth_data, display_name=display_name)

        print(f"\n[INFO] Auth berhasil. Data akun tersimpan di {DB_FILE}.")
        print(f"[INFO] File bot siap pakai: {bot_account}")
        print("[INFO] Refresh token tidak ditampilkan di terminal.")
    except Exception as e:
        print(f"Gagal login: {e}")

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        if len(sys.argv) > 2:
            cmd_get_credentials(sys.argv[2])
        else:
            accounts = load_accounts()
            emails = list(accounts.keys())
            if emails:
                cmd_get_credentials(emails[0])
            else:
                print(f"Tidak ada akun di {DB_FILE}")
    else:
        cmd_generate()

if __name__ == "__main__":
    main()
