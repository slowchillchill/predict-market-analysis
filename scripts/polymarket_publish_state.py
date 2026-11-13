#!/usr/bin/env python3
"""Persist per-platform submission state. Browser confirmation is still required."""
import argparse
import fcntl
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def text_content_sha256(item, platform):
    content = ({'title': item['title'], 'posts': item['posts']} if platform == 'x'
               else {'title': item['title'], 'body': item['body']})
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _validate_text_manifest(manifest, platform):
    if manifest.get('schema_version') != 2 or platform not in ('x', 'linkedin'):
        raise ValueError('Text packages support only X and LinkedIn')
    source = Path(manifest['source_path'])
    if sha256(source) != manifest['source_sha256']:
        raise ValueError('Source changed since manifest creation: source_path')
    bundle = json.loads(source.read_text(encoding='utf-8'))
    if bundle.get('schema_version') != 1 or bundle.get('status') != 'complete':
        raise ValueError('Text source must be a completed cloud report')
    kind = bundle['kind']
    start = date.fromisoformat(bundle['start_date_utc'])
    end = date.fromisoformat(bundle['end_date_utc_exclusive'])
    if kind == 'daily' and end == start + timedelta(days=1):
        target = str(start)
    elif kind == 'weekly' and start.weekday() == 0 and end == start + timedelta(days=7):
        target = f'{start}_{end - timedelta(days=1)}'
    elif (kind == 'monthly' and start.day == 1
          and end == (start.replace(day=28) + timedelta(days=4)).replace(day=1)):
        target = start.strftime('%Y-%m')
    else:
        raise ValueError('Text source requires a complete UTC period')
    valid_bundle_targets = {target, str(start)} if kind == 'weekly' else {target}
    if (bundle.get('target_utc') not in valid_bundle_targets
            or manifest.get('kind') != kind or manifest.get('target_utc') != target
            or manifest.get('start_date_utc') != str(start)
            or manifest.get('end_date_utc_exclusive') != str(end)
            or manifest.get('date_basis') != 'market_end_utc'
            or bundle['payload'].get('date_basis') != 'market_end_utc'):
        raise ValueError('Manifest and source period mismatch')
    item = manifest['platforms'][platform]
    posts = item.get('posts') if platform == 'x' else [item.get('body')]
    if (not isinstance(posts, list) or not posts
            or any(not isinstance(post, str) or not post.strip() for post in posts)
            or item.get('body') != posts[0]):
        raise ValueError('Text package requires nonempty ordered posts')
    paths, hashes = item.get('text_paths', []), item.get('text_sha256s', [])
    if len(paths) != len(posts) or len(hashes) != len(posts):
        raise ValueError('Text package file count differs from its posts')
    for path, expected_hash, post in zip(paths, hashes, posts):
        text = Path(path).read_text(encoding='utf-8')
        if sha256(path) != expected_hash or text != post:
            raise ValueError('Text changed since manifest creation')
    if item.get('content_sha256') != text_content_sha256(item, platform):
        raise ValueError('Text content changed since manifest creation')
    return item


def _validate_legacy_manifest(manifest, platform, status):
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
    return item


def load_manifest(manifest_path):
    """Resolve portable cloud text paths relative to their downloaded manifest."""
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('format') == 'text' and manifest.get('path_base') == 'manifest_directory':
        def resolve(value):
            relative = Path(value)
            if relative.is_absolute() or len(relative.parts) != 1 or relative.name in {'.', '..'}:
                raise ValueError('Portable text paths must be filenames in the manifest directory')
            return str(manifest_path.parent / relative)
        manifest['source_path'] = resolve(manifest['source_path'])
        for item in manifest['platforms'].values():
            item['text_paths'] = [resolve(path) for path in item['text_paths']]
    return manifest


def record(manifest_path, platform, status, *, music=None, post_url=None, post_urls=None,
           evidence=None, screenshot=None, verified_absent=False):
    manifest = load_manifest(manifest_path)
    is_text = manifest.get('format') == 'text'
    item = (_validate_text_manifest(manifest, platform) if is_text
            else _validate_legacy_manifest(manifest, platform, status))
    if post_urls is not None:
        if (not isinstance(post_urls, list) or not post_urls
                or any(not isinstance(url, str) or not url.strip() for url in post_urls)
                or len(set(post_urls)) != len(post_urls)):
            raise ValueError('Post URLs must be a nonempty list of distinct links')
        if post_url is None:
            post_url = post_urls[0]
        elif post_url != post_urls[0]:
            raise ValueError('Post URL differs from first thread URL')
    if is_text and platform == 'x' and status == 'published':
        if post_urls is None or len(post_urls) != len(item['posts']):
            raise ValueError('Published X thread requires a URL for every post')
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
                policy = config.get('legacy_image_music', config.get('music', {}))[platform]
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
              'platform':platform,'status':status,'updated_at':now,
              'text_sha256':hashlib.sha256(item['body'].encode()).hexdigest(),'content_sha256':item['content_sha256'],
              'manifest_path':str(Path(manifest_path).resolve()),'title':item['title'],'body':item['body']}
        if is_text:
            data.update(format='text', text_paths=item['text_paths'], text_sha256s=item['text_sha256s'])
            if platform == 'x':
                data['posts'] = item['posts']
        else:
            data['image_sha256'] = manifest['image_sha256']
        if 'image_paths' in item:
            data['image_paths']=item['image_paths']
            data['image_sha256s']=item['image_sha256s']
            data['image_sha256']=item['image_sha256s'][0]
        if status=='submitting': data['submitted_at']=now
        if status=='published': data['published_at']=old.get('published_at',now)
        for k,v in [('music',music),('post_url',post_url),('post_urls',post_urls),('evidence_path',evidence),('screenshot_path',screenshot)]:
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
    parser.add_argument('--post-urls-json',help='Ordered JSON array of all X thread post URLs')
    parser.add_argument('--post-url'); parser.add_argument('--evidence'); parser.add_argument('--screenshot')
    parser.add_argument('--verified-absent',action='store_true')
    a=parser.parse_args()
    print(record(a.manifest,a.platform,a.status,music=json.loads(a.music_json) if a.music_json else None,post_url=a.post_url,post_urls=json.loads(a.post_urls_json) if a.post_urls_json else None,evidence=a.evidence,screenshot=a.screenshot,verified_absent=a.verified_absent))
