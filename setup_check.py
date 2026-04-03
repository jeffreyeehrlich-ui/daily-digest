import os
import sys
import json
import requests
import subprocess
from dotenv import load_dotenv

load_dotenv()

OWNER = 'jeffreyeehrlich-ui'
REPO = 'daily-digest'
BASE_URL = f'https://api.github.com/repos/{OWNER}/{REPO}'

def check(label, passed, detail=''):
    symbol = '[OK]' if passed else '[ERR]'
    line = f'{symbol} {label}'
    if detail:
        line += f' — {detail}'
    print(line)
    return passed

def run_checks():
    print('\n=== Daily Digest Health Check ===\n')
    results = []

    # Check ANTHROPIC_API_KEY
    key = os.getenv('ANTHROPIC_API_KEY', '')
    results.append(check('ANTHROPIC_API_KEY', bool(key), 'set' if key else 'MISSING'))

    # Check GMAIL_APP_PASSWORD
    pw = os.getenv('GMAIL_APP_PASSWORD', '')
    results.append(check('GMAIL_APP_PASSWORD', bool(pw), 'set' if pw else 'MISSING'))

    # Check FROM_EMAIL and TO_EMAIL
    from_email = os.getenv('FROM_EMAIL', '')
    to_email = os.getenv('TO_EMAIL', '')
    results.append(check('FROM_EMAIL', bool(from_email), from_email or 'MISSING'))
    results.append(check('TO_EMAIL', bool(to_email), to_email or 'MISSING'))

    # Check GITHUB_TOKEN
    github_token = os.getenv('GITHUB_TOKEN', '')
    if github_token:
        headers = {'Authorization': f'token {github_token}',
                   'Accept': 'application/vnd.github.v3+json'}
        r = requests.get(BASE_URL, headers=headers)
        results.append(check('GITHUB_TOKEN', r.status_code == 200,
            'valid' if r.status_code == 200 else f'INVALID ({r.status_code})'))
    else:
        results.append(check('GITHUB_TOKEN', False, 'MISSING'))

    # Check READING_LIST_TOKEN
    rl_token = os.getenv('READING_LIST_TOKEN', '')
    if rl_token:
        headers = {'Authorization': f'token {rl_token}',
                   'Accept': 'application/vnd.github.v3+json'}
        r = requests.get(BASE_URL, headers=headers)
        results.append(check('READING_LIST_TOKEN', r.status_code == 200,
            'valid' if r.status_code == 200 else f'EXPIRED or INVALID ({r.status_code})'))
    else:
        results.append(check('READING_LIST_TOKEN', False, 'MISSING'))

    # Check GitHub Pages
    github_token = os.getenv('GITHUB_TOKEN', '')
    if github_token:
        headers = {'Authorization': f'token {github_token}',
                   'Accept': 'application/vnd.github.v3+json'}
        r = requests.get(f'{BASE_URL}/pages', headers=headers)
        if r.status_code == 200:
            url = r.json().get('html_url', '')
            results.append(check('GitHub Pages', True, f'live at {url}'))
        else:
            results.append(check('GitHub Pages', False, 'NOT ENABLED'))

    # Check digest.py compiles
    result = subprocess.run(['python', '-m', 'py_compile', 'digest.py'],
        capture_output=True)
    results.append(check('digest.py syntax', result.returncode == 0,
        'OK' if result.returncode == 0 else result.stderr.decode()))

    # Check sources.yaml exists
    results.append(check('sources.yaml', os.path.exists('sources.yaml')))

    # Check reading-list/reading-list.json exists
    results.append(check('reading-list/reading-list.json',
        os.path.exists('reading-list/reading-list.json')))

    # Check logs directory
    results.append(check('logs/ directory', os.path.exists('logs')))

    # Check .gitattributes exists
    results.append(check('.gitattributes', os.path.exists('.gitattributes')))

    print(f'\n=== {sum(results)}/{len(results)} checks passed ===\n')
    return all(results)

if __name__ == '__main__':
    success = run_checks()
    sys.exit(0 if success else 1)
