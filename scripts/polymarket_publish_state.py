#!/usr/bin/env python3
"""Persist per-platform submission state. Browser confirmation is still required."""
import argparse
import fcntl
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from polymarket_updown_captions import sha256

ROOT = Path(__file__).resolve().parents[1]


def record(manifest_path, platform, status, *, music=None, post_url=None, evidence=None, screenshot=None, verified_absent=False):
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    for path_key, hash_key in [('image_path','image_sha256'),('source_path','source_sha256'),('english_path','english_sha256')]:
        if sha256(Path(manifest[path_key])) != manifest[hash_key]:
            raise ValueError('Source changed since manifest creation: '+path_key)
    from polymarket_updown_distribution_assets import (load_distribution, report_identity,
                                                       distribution_asset_versions)
    source = Path(manifest['source_path'])
    payload = json.loads(source.read_text(encoding='utf-8'))
    if (Path(manifest['image_path']).resolve() != source.with_suffix('.png').resolve()
            or Path(manifest['english_path']).resolve() != source.with_name(source.stem+'_tweet.md').resolve()):
        raise ValueError('Manifest overview or English caption does not belong to source')
    identity = report_identity(payload)
    kind = identity['kind']
    if manifest.get('kind') != kind or manifest.get('target_utc') != identity['target_utc']:
        raise ValueError('Manifest and source period mismatch')
    distribution = load_distribution(source, payload)
    versions = distribution_asset_versions(payload, kind) if distribution is not None else None
    if status == 'submitting' and distribution is not None:
        from polymarket_updown_buy_metrics import BASIS
        if payload.get('bot_panel_basis') != BASIS or 'buy_event_summary' not in payload:
            raise ValueError('Legacy mixed-basis distribution package cannot be submitted; regenerate BUY-consistent data')
    item=manifest['platforms'][platform]
    if distribution is not None and 'image_paths' not in item:
        raise ValueError('Distribution requires the platform image set')
    if 'image_paths' in item:
        paths, hashes = item['image_paths'], item.get('image_sha256s', [])
        count = (6 if distribution is not None and kind != 'daily' else 4) if platform == 'douyin' else 2
        if len(paths) != count or len(hashes) != count:
            raise ValueError(f'{platform} report requires {count} ordered images')
        if platform == 'douyin':
            expected_version = versions[1] if versions is not None else None
            if distribution is not None:
                if item.get('asset_version') != expected_version:
                    raise ValueError('Unexpected Douyin distribution asset version')
                if ([Path(path).name for path in paths] !=
                        [f'{source.stem}_douyin_{number:02}.png' for number in range(1, count+1)]
                        or len({str(Path(path).parent) for path in paths}) != 1):
                    raise ValueError('Douyin distribution card order mismatch')
        else:
            if versions is None or item.get('asset_version') != versions[0]:
                raise ValueError('Unexpected platform image set')
            expected_paths = [manifest['image_path'], str(source.parent / payload['trade_distribution']['image_file'])]
            if paths != expected_paths:
                raise ValueError('Distribution image order mismatch')
        from PIL import Image
        for image_path, expected in zip(paths, hashes):
            if sha256(Path(image_path)) != expected:
                raise ValueError(('Douyin card' if platform == 'douyin' else 'Platform image') + ' changed since manifest creation')
            with Image.open(image_path) as image:
                image.verify()
        actual = hashlib.sha256(json.dumps({'title':item['title'],'body':item['body'],
                    'image_sha256s':hashes},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        if actual != item['content_sha256']:
            raise ValueError(('Douyin card' if platform == 'douyin' else 'Platform image') + ' order or caption changed since manifest creation')
    folder=ROOT/'outputs/publish_state'; folder.mkdir(parents=True,exist_ok=True)
    key=f'{manifest["kind"]}_{manifest["target_utc"]}_{platform}'
    path=folder/(key+'.json')
    with (folder/(key+'.lock')).open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        old=json.loads(path.read_text()) if path.exists() else {}
        if status == 'submitting':
            if old.get('status') in ('published','pending_review','submitting','unknown'):
                raise ValueError('Existing submission must be reconciled, never blindly resubmitted')
            if not verified_absent:
                raise ValueError('Check platform recent posts, then pass --verified-absent')
            if platform in ('douyin','tiktok'):
                config=json.loads(Path(manifest['music_config']).read_text())
                policy = config['music'][platform]
                complete = isinstance(music, dict) and all(isinstance(music.get(k), str) and music[k].strip() for k in ('title', 'artist', 'duration'))
                matched = complete and any(all(music[k] == candidate[k] for k in ('title', 'artist', 'duration')) for candidate in policy['candidates'])
                fallback = complete and platform == 'douyin' and policy.get('on_unavailable') == 'select_available' and music.get('selection_reason') == 'configured_candidates_unavailable'
                if not (matched or fallback):
                    raise ValueError('Music not in configured candidates')
        elif status == 'published' and (not post_url or not evidence):
            raise ValueError('Published requires URL and browser evidence')
        elif status not in ('submitting','pending_review','published','unknown','blocked','failed'):
            raise ValueError('Invalid status')
        if old.get('status') == 'published' and status != 'published':
            raise ValueError('Never downgrade a published record')
        if old.get('content_sha256') and old['content_sha256'] != item['content_sha256']:
            raise ValueError('Content changed after recording; reconcile first')
        now=datetime.now(timezone.utc).isoformat()
        data={**old,'key':key.replace('_','+'),'kind':manifest['kind'],'target_utc':manifest['target_utc'],
              'platform':platform,'status':status,'updated_at':now,'image_sha256':manifest['image_sha256'],
              'text_sha256':hashlib.sha256(item['body'].encode()).hexdigest(),'content_sha256':item['content_sha256'],
              'manifest_path':str(Path(manifest_path).resolve()),'title':item['title'],'body':item['body']}
        if 'image_paths' in item:
            data['image_paths']=item['image_paths']
            data['image_sha256s']=item['image_sha256s']
            data['image_sha256']=item['image_sha256s'][0]
        if status=='submitting': data['submitted_at']=now
        if status=='published': data['published_at']=old.get('published_at',now)
        for k,v in [('music',music),('post_url',post_url),('evidence_path',evidence),('screenshot_path',screenshot)]:
            if v is not None: data[k]=v
        data.setdefault('history',[]).append({'status':status,'at':now})
        temporary=path.with_suffix('.json.tmp'); temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n'); temporary.replace(path)
    return path


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True,type=Path)
    parser.add_argument('--platform',required=True,choices=['x','linkedin','douyin','tiktok'])
    parser.add_argument('--status',required=True,choices=['submitting','pending_review','published','unknown','blocked','failed'])
    parser.add_argument('--music-json',help='JSON object with selected title, artist, duration')
    parser.add_argument('--post-url'); parser.add_argument('--evidence'); parser.add_argument('--screenshot')
    parser.add_argument('--verified-absent',action='store_true')
    a=parser.parse_args()
    print(record(a.manifest,a.platform,a.status,music=json.loads(a.music_json) if a.music_json else None,post_url=a.post_url,evidence=a.evidence,screenshot=a.screenshot,verified_absent=a.verified_absent))
