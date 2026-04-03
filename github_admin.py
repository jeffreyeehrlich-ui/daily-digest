import os
import json
import base64
import requests
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
OWNER = 'jeffreyeehrlich-ui'
REPO = 'daily-digest'
BASE_URL = f'https://api.github.com/repos/{OWNER}/{REPO}'
HEADERS = {
    'Authorization': f'token {GITHUB_TOKEN}',
    'Accept': 'application/vnd.github.v3+json',
    'Content-Type': 'application/json'
}

def enable_github_pages():
    try:
        r = requests.put(f'{BASE_URL}/pages', headers=HEADERS,
            json={'source': {'branch': 'main', 'path': '/'}})
        if r.status_code in [200, 201, 204]:
            print('[OK] GitHub Pages enabled on main branch')
        elif r.status_code == 409:
            print('[OK] GitHub Pages already enabled')
        else:
            print(f'[ERR] GitHub Pages failed: {r.status_code} {r.text}')
    except Exception as e:
        print(f'[ERR] GitHub Pages error: {e}')

def set_workflow_permissions():
    try:
        r = requests.put(f'{BASE_URL}/actions/permissions/workflow',
            headers=HEADERS,
            json={'default_workflow_permissions': 'write',
                  'can_approve_pull_request_reviews': True})
        if r.status_code in [200, 204]:
            print('[OK] Workflow permissions set to read/write')
        else:
            print(f'[ERR] Workflow permissions failed: {r.status_code} {r.text}')
    except Exception as e:
        print(f'[ERR] Workflow permissions error: {e}')

def trigger_workflow(workflow_filename):
    try:
        r = requests.post(
            f'{BASE_URL}/actions/workflows/{workflow_filename}/dispatches',
            headers=HEADERS,
            json={'ref': 'main'})
        if r.status_code == 204:
            print(f'[OK] Workflow {workflow_filename} triggered successfully')
        else:
            print(f'[ERR] Workflow trigger failed: {r.status_code} {r.text}')
    except Exception as e:
        print(f'[ERR] Workflow trigger error: {e}')

def get_workflow_status(workflow_filename):
    try:
        r = requests.get(
            f'{BASE_URL}/actions/workflows/{workflow_filename}/runs',
            headers=HEADERS,
            params={'per_page': 1})
        if r.status_code == 200:
            runs = r.json().get('workflow_runs', [])
            if runs:
                run = runs[0]
                print(f'Workflow: {workflow_filename}')
                print(f'Status: {run["status"]}')
                print(f'Conclusion: {run["conclusion"]}')
                print(f'Started: {run["created_at"]}')
                return run
            else:
                print(f'No runs found for {workflow_filename}')
        else:
            print(f'[ERR] Could not get workflow status: {r.status_code}')
    except Exception as e:
        print(f'[ERR] Workflow status error: {e}')

def check_pages_status():
    try:
        r = requests.get(f'{BASE_URL}/pages', headers=HEADERS)
        if r.status_code == 200:
            data = r.json()
            print(f'[OK] GitHub Pages is live at: {data.get("html_url")}')
            print(f'   Status: {data.get("status")}')
            print(f'   Branch: {data.get("source", {}).get("branch")}')
            return data
        elif r.status_code == 404:
            print('[ERR] GitHub Pages is not enabled')
        else:
            print(f'[ERR] Pages status check failed: {r.status_code}')
    except Exception as e:
        print(f'[ERR] Pages status error: {e}')

def create_or_update_secret(secret_name, secret_value):
    try:
        from nacl import encoding, public
        key_response = requests.get(
            f'{BASE_URL}/actions/secrets/public-key',
            headers=HEADERS)
        if key_response.status_code != 200:
            print(f'[ERR] Could not get public key: {key_response.status_code}')
            return
        key_data = key_response.json()
        public_key = public.PublicKey(
            key_data['key'].encode('utf-8'),
            encoding.Base64Encoder())
        sealed_box = public.SealedBox(public_key)
        encrypted = sealed_box.encrypt(
            secret_value.encode('utf-8'))
        encrypted_value = base64.b64encode(encrypted).decode('utf-8')
        r = requests.put(
            f'{BASE_URL}/actions/secrets/{secret_name}',
            headers=HEADERS,
            json={
                'encrypted_value': encrypted_value,
                'key_id': key_data['key_id']
            })
        if r.status_code in [201, 204]:
            print(f'[OK] Secret {secret_name} created/updated successfully')
        else:
            print(f'[ERR] Secret creation failed: {r.status_code} {r.text}')
    except ImportError:
        print('[ERR] PyNaCl not installed — run: pip install PyNaCl')
    except Exception as e:
        print(f'[ERR] Secret creation error: {e}')

def validate_token():
    try:
        r = requests.get(f'{BASE_URL}', headers=HEADERS)
        if r.status_code == 200:
            print(f'[OK] GITHUB_TOKEN is valid')
            return True
        else:
            print(f'[ERR] GITHUB_TOKEN is invalid or expired: {r.status_code}')
            return False
    except Exception as e:
        print(f'[ERR] Token validation error: {e}')
        return False

if __name__ == '__main__':
    import sys
    commands = {
        'pages':       enable_github_pages,
        'permissions': set_workflow_permissions,
        'status':      check_pages_status,
        'validate':    validate_token,
    }
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == 'trigger' and len(sys.argv) > 2:
            trigger_workflow(sys.argv[2])
        elif cmd == 'status-workflow' and len(sys.argv) > 2:
            get_workflow_status(sys.argv[2])
        elif cmd in commands:
            commands[cmd]()
        else:
            print('Available commands: pages, permissions, status, validate, trigger <workflow.yml>, status-workflow <workflow.yml>')
    else:
        print('Available commands: pages, permissions, status, validate, trigger <workflow.yml>, status-workflow <workflow.yml>')
