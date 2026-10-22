"""Upload an existing Mac jobs cache; never opens a browser or requests job sites.

Run with HERMES_VM_HOST and HERMES_VM_KEY from the existing private configuration.
--launch-agent prints a plist for hourly uploads of changed cache data and an
initial check at login. Installing/loading it is a separate review step.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
import uuid


def sync(cache, state, host, key, run=subprocess.run):
    cache, state, key = Path(cache), Path(state), Path(key)
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*', host or '') or not key.is_file():
        raise ValueError('Valid configured host and existing SSH key required')
    if cache.stat().st_size > 10_000_000:
        raise ValueError('Browser cache exceeds size limit')
    raw = cache.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get('sources'), dict) or not data['sources']:
        raise ValueError('No valid browser-source cache to upload')
    digest = hashlib.sha256(host.encode() + b'\0' + raw).hexdigest()
    state.parent.mkdir(parents=True, exist_ok=True)
    with state.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'another upload is in progress'
        try:
            if state.read_text().strip() == digest:
                return 'unchanged; no connection made'
        except FileNotFoundError:
            pass
        token = uuid.uuid4().hex
        staging = '/home/azureuser/browser_jobs.sync.' + token + '.json'
        target = '/home/hermes/app/browser_jobs.json'
        temporary_target = target + '.' + token
        options = ['-i', str(key), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=yes']
        # Freeze the complete bytes in case the daily reader replaces its cache
        # between calculating the hash and starting scp.
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)/'browser_jobs.json'
            snapshot.write_bytes(raw)
            run(['scp', *options, '-q', str(snapshot), 'azureuser@'+host+':'+staging], check=True, timeout=30)
            command = ('set -eu; trap ' + shlex.quote('rm -f '+shlex.quote(staging)) + ' EXIT; ' +
                       'sudo -n install -o hermes -g hermes -m 644 '+shlex.quote(staging)+' '+shlex.quote(temporary_target)+'; '+
                       'sudo -n mv -f '+shlex.quote(temporary_target)+' '+shlex.quote(target))
            run(['ssh', *options, 'azureuser@'+host, command], check=True, timeout=30)
        # An unsuccessful upload never advances the marker. A later scheduled
        # invocation may retry the file transfer, never any careers request.
        fd, tmp = tempfile.mkstemp(prefix='.'+state.name, dir=state.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(digest+'\n')
            os.replace(tmp, state)
        finally:
            if Path(tmp).exists():
                Path(tmp).unlink()
        return 'changed cache uploaded atomically'


def launch_agent():
    home = Path.home()
    command = ('source '+shlex.quote(str(home/'.hermes-browser/config.env'))+'; '+
               'export HERMES_VM_HOST HERMES_VM_KEY; exec '+shlex.quote(sys.executable)+' '+shlex.quote(str(Path(__file__).resolve())))
    return {'Label':'com.hermes.browser-cache-sync', 'ProgramArguments':['/bin/bash','-c',command],
            'StartInterval':3600, 'RunAtLoad':True,
            'StandardOutPath':str(home/'.hermes-browser/cache-sync.log'),
            'StandardErrorPath':str(home/'.hermes-browser/cache-sync.log')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launch-agent', action='store_true')
    args = parser.parse_args()
    if args.launch_agent:
        sys.stdout.buffer.write(plistlib.dumps(launch_agent()))
        return
    home = Path.home()/'.hermes-browser'
    print(sync(home/'browser_jobs.json', home/'cache-sync.sha256',
               os.environ.get('HERMES_VM_HOST',''), os.environ.get('HERMES_VM_KEY','')))


if __name__ == '__main__':
    main()
