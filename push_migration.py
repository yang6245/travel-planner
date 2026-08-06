# -*- coding: utf-8 -*-
"""
推送迁移结果到 GitHub（一次性）：
1. 读取 token：优先环境变量 GH_TOKEN，否则读取 .gh_token.txt（首行）
2. 上传 images/MiletoPuzhehei/*.jpg（30 张，每张 <1MB，contents API 直连 PUT）
3. 更新 data/MiletoPuzhehei/trip.json（URL 版 15.6KB，带 sha）
4. 验证：API size 应 < 1MB；jsDelivr 抽验一张图 URL
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

OWNER = 'yang6245'
REPO = 'travel-planner'
BRANCH = 'main'
BASE = 'https://api.github.com'
ROOT = 'F:/开发者程序/旅游规划小程序'
OUT = os.path.join(ROOT, '_migrate_out')
TRIP_DIR = 'MiletoPuzhehei'

def get_token():
    t = os.environ.get('GH_TOKEN', '')
    if t:
        return t.strip()
    fp = os.path.join(ROOT, '.gh_token.txt')
    if os.path.isfile(fp):
        t = open(fp, encoding='utf-8').read().strip().splitlines()
        return t[0].strip() if t else ''
    return ''

def api(method, path, body=None, retries=3):
    url = BASE + path
    data = json.dumps(body).encode('utf-8') if body is not None else None
    for i in range(retries + 1):
        req = urllib.request.Request(url, method=method, data=data)
        req.add_header('Authorization', 'Bearer ' + TOKEN)
        req.add_header('Accept', 'application/vnd.github.v3+json')
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                err = e.read().decode('utf-8', errors='ignore')
                return e.code, {'error': err[:300]}
            if i < retries:
                time.sleep(1.5 * (i + 1))
        except Exception as e:
            if i < retries:
                time.sleep(1.5 * (i + 1))
            else:
                raise
    raise RuntimeError('API 重试耗尽')

TOKEN = get_token()
if not TOKEN:
    print('❌ 未找到 token：请把 GitHub token 写入 %s 后重跑' % os.path.join(ROOT, '.gh_token.txt'))
    sys.exit(1)
print('✅ token 已加载（长度 %d，不展示内容）' % len(TOKEN))

# 1. 上传 30 张图片
img_dir = os.path.join(OUT, 'images', TRIP_DIR)
files = sorted(os.listdir(img_dir))
ok = fail = 0
for fname in files:
    fpath = os.path.join(img_dir, fname)
    b64 = base64.b64encode(open(fpath, 'rb').read()).decode('utf-8')
    gpath = '/repos/%s/%s/contents/images/%s/%s' % (OWNER, REPO, TRIP_DIR, fname)
    st, resp = api('PUT', gpath, {
        'message': 'upload trip image %s/%s' % (TRIP_DIR, fname),
        'content': b64, 'branch': BRANCH,
    })
    if st in (200, 201):
        ok += 1
        print('  ✓ %s (%d KB)' % (fname, os.path.getsize(fpath) // 1024))
    else:
        fail += 1
        print('  ✗ %s -> %s %s' % (fname, st, resp.get('error', '')[:120]))
print('图片上传完成: 成功 %d / 失败 %d' % (ok, fail))

# 2. 更新 trip.json（先取当前 sha）
gpath = '/repos/%s/%s/contents/data/%s/trip.json' % (OWNER, REPO, TRIP_DIR)
st, resp = api('GET', gpath)
sha = resp.get('sha', '')
print('trip.json 当前 sha: %s, size: %s' % (sha[:10], resp.get('size')))
trip_path = os.path.join(OUT, 'data', TRIP_DIR, 'trip.json')
content_b64 = base64.b64encode(open(trip_path, 'rb').read()).decode('utf-8')
st, resp = api('PUT', gpath, {
    'message': 'migrate images to external CDN (trip.json only URLs)',
    'content': content_b64, 'branch': BRANCH, 'sha': sha,
})
if st in (200, 201):
    print('✅ trip.json 更新成功, 新 sha: %s' % resp.get('content', {}).get('sha', '')[:10])
else:
    print('❌ trip.json 更新失败: %s %s' % (st, resp.get('error', '')))

# 3. 验证
st, resp = api('GET', gpath)
print('\n=== 验证 ===')
print('云端 trip.json size: %s bytes = %.2f MB (%s)' % (
    resp.get('size'), (resp.get('size') or 0) / 1048576, '✅ <1MB' if (resp.get('size') or 0) < 1048576 else '❌ 仍超限'))
if ok > 0:
    sample = files[0]
    url = 'https://cdn.jsdelivr.net/gh/%s/%s@%s/images/%s/%s' % (OWNER, REPO, BRANCH, TRIP_DIR, sample)
    print('抽验图片 URL: %s' % url)
