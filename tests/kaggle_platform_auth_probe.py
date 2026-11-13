"""Explicit cloud-only synthetic permission probe; never print credential values."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    if not Path('/kaggle/working').is_dir():
        raise RuntimeError('This probe runs only on Kaggle')
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet',
                    'kaggle==2.2.5', 'kagglesdk==0.1.38', 'six==1.17.0'], check=True)
    from kagglesdk import get_access_token_from_env
    token, source = get_access_token_from_env()
    del token
    evidence = {'source': source, 'production_writes': 0, 'operations': []}
    destination = Path('/kaggle/working/auth_probe.json')
    def run(*args):
        result = subprocess.run(['kaggle', *args], capture_output=True, text=True, timeout=120)
        evidence['operations'].append({'operation': list(args[:2]), 'returncode': result.returncode,
                                       'output': (result.stdout + result.stderr)[-4000:]})
        destination.write_text(json.dumps(evidence, indent=2))
        print(args[:2], 'returncode', result.returncode, flush=True)
        return result
    if source != 'KAGGLE_API_V1_TOKEN':
        evidence['status'] = 'platform_credential_unavailable'
        destination.write_text(json.dumps(evidence, indent=2))
        return
    slug = 'qa-cloud-auth-20261113-' + str(int(time.time()))
    ref = 'qiuqiuslowchillchill/' + slug
    evidence['dataset'] = ref
    folder = Path('/kaggle/working/auth-snapshot')
    folder.mkdir()
    (folder/'dataset-metadata.json').write_text(json.dumps({'id':ref, 'title':slug,
        'licenses':[{'name':'CC0-1.0'}]}))
    (folder/'sample.json').write_text('{"phase":1}\n')
    created = run('datasets','create','-p',str(folder),'--keep-tabular','--quiet')
    if created.returncode or 'creation error:' in (created.stdout + created.stderr):
        evidence['status'] = 'create_failed'
    else:
        for _ in range(12):
            status = run('datasets','status',ref,'--format','json')
            if status.returncode == 0 and json.loads(status.stdout)['status'].lower() == 'ready':
                break
            time.sleep(10)
        (folder/'sample.json').write_text('{"phase":2}\n')
        run('datasets','version','-p',str(folder),'-m','Native auth update probe','--keep-tabular','--quiet')
        for _ in range(12):
            status = run('datasets','status',ref,'--format','json')
            if status.returncode == 0:
                state = json.loads(status.stdout)
                if state['status'].lower() == 'ready' and state['current_version_number'] == 2:
                    break
            time.sleep(10)
        run('datasets','download',ref+'/2','-f','sample.json','-p','/kaggle/working/readback','--unzip','--quiet')
        evidence['status'] = 'probe_finished'
    destination.write_text(json.dumps(evidence, indent=2))
    print('Probe evidence saved; credential values were not printed', flush=True)


if __name__ == '__main__':
    main()
