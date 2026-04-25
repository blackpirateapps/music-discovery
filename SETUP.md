# Music Discovery — Setup Guide

## Prerequisites

- Python 3.10+
- A [ListenBrainz](https://listenbrainz.org) account with the API enabled
- A Google Cloud project with the **Google Drive API** enabled
- A target Google Drive folder (you'll need its folder ID)

---

## Step 1 — ListenBrainz API Token

1. Log in at [listenbrainz.org](https://listenbrainz.org).
2. Go to **Profile → Music Services & API Token**.
3. Copy your **User Token**.

---

## Step 2 — Google Cloud OAuth2 Setup

### 2a. Create a project & enable Drive API

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Create a new project (e.g. `music-discovery`).
3. Navigate to **APIs & Services → Library** and enable **Google Drive API**.

### 2b. Create OAuth credentials

1. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Application type: **Desktop app**.
3. Download **`credentials.json`** and place it in the project root.

---

## Step 3 — Generate `token.json` (one-time, local)

Install dependencies locally:

```bash
pip install google-auth-oauthlib
```

Run the following one-time script to authorize your account and obtain a refresh token:

```python
# generate_token.py  — run once on your local machine
from google_auth_oauthlib.flow import InstalledAppFlow
import json

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
creds = flow.run_local_server(port=0)

# Write token.json
token_data = {
    "token":         creds.token,
    "refresh_token": creds.refresh_token,
    "token_uri":     creds.token_uri,
    "client_id":     creds.client_id,
    "client_secret": creds.client_secret,
    "scopes":        list(creds.scopes),
}
with open("token.json", "w") as f:
    json.dump(token_data, f, indent=2)

print("token.json written successfully.")
```

```bash
python generate_token.py
```

This opens a browser window. Authorize the app. `token.json` is created locally.

---

## Step 4 — Find your Google Drive Folder ID

Open the target folder in Google Drive.  
The URL will look like: `https://drive.google.com/drive/folders/<FOLDER_ID>`  
Copy the `<FOLDER_ID>` part.

---

## Step 5 — Add GitHub Secrets

In your GitHub repository, go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name        | Value                                      |
|--------------------|--------------------------------------------|
| `LB_TOKEN`         | Your ListenBrainz API token                |
| `LB_USERNAME`      | Your ListenBrainz username                 |
| `GDRIVE_FOLDER_ID` | The Drive folder ID from Step 4            |
| `GDRIVE_CREDENTIALS` | Full JSON content of `credentials.json` |
| `GDRIVE_TOKEN`     | Full JSON content of `token.json`          |

To get the JSON content as a single line for copy-paste:

```bash
cat credentials.json | tr -d '\n'
cat token.json       | tr -d '\n'
```

---

## Step 6 — Verify

Push to `main`. The workflow runs automatically at **04:30 UTC** every day.  
You can also trigger it manually via **Actions → Music Discovery Sync → Run workflow**.

---

## Local Test Run

```bash
export LB_TOKEN="your_lb_token"
export LB_USERNAME="your_lb_username"
export GDRIVE_FOLDER_ID="your_folder_id"
export GDRIVE_CREDENTIALS="$(cat credentials.json)"
export GDRIVE_TOKEN="$(cat token.json)"

pip install -r requirements.txt
python main.py
```

---

## Token Refresh Notes

- The script automatically refreshes the OAuth access token if it has expired using the `refresh_token`.
- You **do not** need to regenerate `token.json` after the initial setup, as long as the refresh token remains valid.
- Refresh tokens can be revoked if the OAuth consent screen is set to "Testing" and not verified. Consider publishing the app or adding your account as a test user.
